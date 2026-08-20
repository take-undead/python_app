"""カメラ 1 台分のタイル。IP 入力・接続操作・映像表示をまとめる。

接続処理は別スレッドで行い、結果は MainWindow と共有するキューに入れる。
UI への反映は MainWindow の定期処理（after）から行われるため、
このクラスのワーカースレッドはウィジェットに触らない。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageTk

from logic import api
from logic.api import ApiError
from logic.stream import MjpegStream, StreamError
from ui.imaging import fit_image, frame_to_image

# タイルの枠の色（選択中／非選択）
_BORDER_SELECTED = "#1a73e8"
_BORDER_NORMAL = "#c8c8c8"

# 映像表示部の背景と文字色
_VIDEO_BACKGROUND = "#202020"
_VIDEO_FOREGROUND = "#f0f0f0"


class CameraTile(tk.Frame):
    """カメラ 1 台分の表示と接続操作。"""

    def __init__(
        self,
        master: tk.Misc,
        index: int,
        host: str,
        task_queue: queue.Queue,
        on_select: Callable[["CameraTile"], None],
        on_connect_request: Callable[["CameraTile"], None],
        on_connect_result: Callable[["CameraTile", str | None], None],
        on_status: Callable[[str], None],
    ) -> None:
        super().__init__(
            master,
            highlightthickness=2,
            highlightbackground=_BORDER_NORMAL,
            highlightcolor=_BORDER_NORMAL,
        )

        self.index = index
        self.host_var = tk.StringVar(value=host)

        self._queue = task_queue
        self._on_select = on_select
        self._on_connect_request = on_connect_request
        self._on_connect_result = on_connect_result
        self._on_status = on_status

        self._stream: MjpegStream | None = None
        self._photo: ImageTk.PhotoImage | None = None  # GC 防止のため参照を保持
        self._connecting = False

        self._build_widgets()

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, padding=(4, 3))
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(1, weight=1)

        self._name_label = ttk.Label(header, text=self.label, width=8)
        self._name_label.grid(row=0, column=0, sticky="w")

        self._host_entry = ttk.Entry(header, textvariable=self.host_var, width=14)
        self._host_entry.grid(row=0, column=1, sticky="ew", padx=(0, 4))

        self._connect_button = ttk.Button(
            header,
            text="接続",
            width=5,
            command=lambda: self._on_connect_request(self),
        )
        self._connect_button.grid(row=0, column=2, padx=(0, 2))

        self._disconnect_button = ttk.Button(
            header, text="切断", width=5, state="disabled", command=self.disconnect
        )
        self._disconnect_button.grid(row=0, column=3)

        self._video_label = tk.Label(
            self,
            background=_VIDEO_BACKGROUND,
            foreground=_VIDEO_FOREGROUND,
            font=("Meiryo UI", 9),
            text="未接続",
            anchor="center",
        )
        self._video_label.grid(row=1, column=0, columnspan=2, sticky="nsew")

        # タイル内のどこをクリックしても、そのカメラを操作対象にする
        for widget in (self, header, self._name_label, self._video_label):
            widget.bind("<Button-1>", self._on_click)

    def _on_click(self, _event: tk.Event) -> None:
        self._on_select(self)

    # ------------------------------------------------------------------
    # 状態
    # ------------------------------------------------------------------
    @property
    def label(self) -> str:
        return f"カメラ {self.index + 1}"

    @property
    def is_connected(self) -> bool:
        return self._stream is not None

    @property
    def is_connecting(self) -> bool:
        return self._connecting

    def host(self) -> str:
        """入力された IP アドレスを正規化して返す。不正なら ApiError。"""
        return api.normalize_host(self.host_var.get())

    def set_selected(self, selected: bool) -> None:
        color = _BORDER_SELECTED if selected else _BORDER_NORMAL
        self.configure(highlightbackground=color, highlightcolor=color)

    # ------------------------------------------------------------------
    # 接続・切断
    # ------------------------------------------------------------------
    def connect(self) -> None:
        """映像への接続を開始する。結果は on_connect_result で必ず 1 度通知する。"""
        if self._stream is not None or self._connecting:
            self._on_connect_result(self, None)
            return

        try:
            host = self.host()
        except ApiError as exc:
            self._on_connect_result(self, str(exc))
            return

        self.host_var.set(host)
        url = api.stream_url(host)

        self._connecting = True
        self._connect_button.configure(state="disabled")
        self._host_entry.configure(state="disabled")
        self._show_message("接続しています...")

        def worker() -> None:
            stream = MjpegStream(url)
            try:
                # 接続確立までブロックするため別スレッドで実行する
                stream.start()
            except StreamError as exc:
                self._queue.put((self._on_failed, exc))
            else:
                self._queue.put((self._on_connected, stream))

        threading.Thread(
            target=worker, name=f"esp32cam-connect-{self.index}", daemon=True
        ).start()

    def _on_connected(self, stream: MjpegStream) -> None:
        self._connecting = False
        self._stream = stream
        self._disconnect_button.configure(state="normal")
        self._show_message("映像を待っています...")
        self._on_connect_result(self, None)

    def _on_failed(self, exc: StreamError) -> None:
        self._connecting = False
        self._connect_button.configure(state="normal")
        self._host_entry.configure(state="normal")
        self._show_message("接続できませんでした")
        self._on_connect_result(self, str(exc))

    def disconnect(self) -> None:
        """接続していれば切断する。していなければ何もしない。"""
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.stop()

        self._photo = None
        self._connect_button.configure(state="disabled" if self._connecting else "normal")
        self._disconnect_button.configure(state="disabled")
        self._host_entry.configure(state="disabled" if self._connecting else "normal")
        self._show_message("未接続")

    # ------------------------------------------------------------------
    # 定期処理（MainWindow の after から呼ばれる）
    # ------------------------------------------------------------------
    def poll(self) -> None:
        stream = self._stream
        if stream is None:
            return

        error = stream.take_error()
        if error is not None:
            # 6 台分のダイアログが積み重なると操作できなくなるため、
            # 受信中のエラーはタイルとステータスバーに出すだけにする
            self.disconnect()
            self._show_message("切断されました")
            self._on_status(f"{self.label}: {error}")
            return

        frame = stream.take_frame()
        if frame is not None:
            self._show_frame(frame)

    def _show_frame(self, frame: np.ndarray) -> None:
        image = fit_image(
            frame_to_image(frame),
            self._video_label.winfo_width(),
            self._video_label.winfo_height(),
            resample=Image.BILINEAR,  # 6 枚を毎回変換するため軽い方式にする
        )
        photo = ImageTk.PhotoImage(image)
        self._photo = photo
        self._video_label.configure(image=photo, text="")

    def _show_message(self, text: str) -> None:
        self._photo = None
        self._video_label.configure(image="", text=text)

    # ------------------------------------------------------------------
    # 後始末
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        self.disconnect()
