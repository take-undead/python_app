"""メイン画面。ESP32-CAM の映像表示・撮影・SD カード内の写真閲覧を行う。"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

import numpy as np
from PIL import ImageTk

from logic import api, settings
from logic.api import ApiError
from logic.stream import MjpegStream, StreamError
from ui.imaging import fit_image, frame_to_image
from ui.photo_preview import PhotoPreview

# 画面更新の間隔（ミリ秒）。約 30 fps
_INTERVAL_MS = 33

# PC に写真を保存するときの既定フォルダ
DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"


class MainWindow(ttk.Frame):
    """ESP32-CAM ビューアのメイン画面。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=8)

        self._stream: MjpegStream | None = None
        self._photo: ImageTk.PhotoImage | None = None  # GC 防止のため参照を保持
        self._tick_id: str | None = None
        self._queue: queue.Queue[tuple[Callable[[Any], None], Any]] = queue.Queue()
        self._busy = False

        self._host_var = tk.StringVar(value=settings.load_host())
        self._flash_var = tk.BooleanVar(value=False)
        self._status_var = tk.StringVar(
            value="IP アドレスを確認して「接続」を押してください。"
        )

        self._build_widgets()
        self._tick()

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        control = ttk.Frame(self)
        control.grid(row=0, column=0, sticky="ew")

        ttk.Label(control, text="IP:").grid(row=0, column=0, padx=(0, 4))

        self._host_entry = ttk.Entry(control, textvariable=self._host_var, width=16)
        self._host_entry.grid(row=0, column=1, padx=(0, 8))

        self._connect_button = ttk.Button(
            control, text="接続", command=self._on_connect, width=8
        )
        self._connect_button.grid(row=0, column=2, padx=(0, 4))

        self._disconnect_button = ttk.Button(
            control, text="切断", command=self._on_disconnect, width=8, state="disabled"
        )
        self._disconnect_button.grid(row=0, column=3, padx=(0, 12))

        self._flash_check = ttk.Checkbutton(
            control, text="フラッシュ", variable=self._flash_var
        )
        self._flash_check.grid(row=0, column=4, padx=(0, 8))

        self._capture_button = ttk.Button(
            control, text="撮影", command=self._on_capture, width=8
        )
        self._capture_button.grid(row=0, column=5, padx=(0, 4))

        self._status_button = ttk.Button(
            control, text="状態", command=self._on_status, width=8
        )
        self._status_button.grid(row=0, column=6)

        # 右端に余白を寄せて、操作部を左詰めにする
        control.columnconfigure(7, weight=1)

        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew", pady=8)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0)
        body.rowconfigure(0, weight=1)

        self._video_label = tk.Label(
            body,
            background="#202020",
            foreground="#f0f0f0",
            font=("Meiryo UI", 11),
            text="未接続",
        )
        self._video_label.grid(row=0, column=0, sticky="nsew")

        self._build_photo_panel(body)

        status = ttk.Label(self, textvariable=self._status_var, anchor="w")
        status.grid(row=2, column=0, sticky="ew")

    def _build_photo_panel(self, master: tk.Misc) -> None:
        """SD カード内の写真を扱う右側のパネルを作る。"""
        panel = ttk.Frame(master, padding=(8, 0, 0, 0))
        panel.grid(row=0, column=1, sticky="ns")
        panel.rowconfigure(1, weight=1)

        ttk.Label(panel, text="SD カード内の写真").grid(row=0, column=0, sticky="w")

        list_frame = ttk.Frame(panel)
        list_frame.grid(row=1, column=0, sticky="nsew", pady=4)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        # ttk には一覧表示用のリストボックスが無いため tk.Listbox を使う
        self._photo_list = tk.Listbox(
            list_frame, width=28, font=("Meiryo UI", 9), exportselection=False
        )
        self._photo_list.grid(row=0, column=0, sticky="nsew")
        self._photo_list.bind("<Double-Button-1>", lambda _event: self._on_show_photo())

        scrollbar = ttk.Scrollbar(
            list_frame, orient="vertical", command=self._photo_list.yview
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._photo_list.configure(yscrollcommand=scrollbar.set)

        buttons = ttk.Frame(panel)
        buttons.grid(row=2, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)

        self._refresh_button = ttk.Button(
            buttons, text="一覧更新", command=self._on_refresh_photos
        )
        self._refresh_button.grid(row=0, column=0, sticky="ew", padx=(0, 2))

        self._show_button = ttk.Button(
            buttons, text="表示", command=self._on_show_photo
        )
        self._show_button.grid(row=0, column=1, sticky="ew", padx=(2, 0))

    # ------------------------------------------------------------------
    # 非同期処理（通信は必ず別スレッドで行い、結果を after 経由で受け取る）
    # ------------------------------------------------------------------
    def _run(
        self,
        task: Callable[[], Any],
        on_success: Callable[[Any], None],
        busy_message: str,
    ) -> None:
        if self._busy:
            return

        self._set_busy(True)
        self._status_var.set(busy_message)

        def worker() -> None:
            try:
                result = task()
            except (ApiError, StreamError) as exc:
                self._queue.put((self._on_task_error, exc))
            else:
                self._queue.put((on_success, result))

        threading.Thread(target=worker, name="esp32cam-task", daemon=True).start()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self._capture_button,
            self._status_button,
            self._refresh_button,
            self._show_button,
        ):
            button.configure(state=state)
        if self._stream is None:
            self._connect_button.configure(state=state)

    def _on_task_error(self, exc: Exception) -> None:
        self._status_var.set("エラーが発生しました。")
        messagebox.showerror("通信エラー", str(exc), parent=self)

    def _drain_queue(self) -> None:
        while True:
            try:
                callback, result = self._queue.get_nowait()
            except queue.Empty:
                return
            self._set_busy(False)
            callback(result)

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def _on_connect(self) -> None:
        if self._stream is not None:
            return

        try:
            host = api.normalize_host(self._host_var.get())
        except ApiError as exc:
            messagebox.showerror("入力エラー", str(exc), parent=self)
            return

        self._host_var.set(host)
        settings.save_host(host)
        url = api.stream_url(host)

        def task() -> MjpegStream:
            stream = MjpegStream(url)
            stream.start()  # 接続確立までブロックするため別スレッドで実行する
            return stream

        self._run(task, self._on_connected, f"接続しています: {url}")

    def _on_connected(self, stream: MjpegStream) -> None:
        self._stream = stream
        self._connect_button.configure(state="disabled")
        self._disconnect_button.configure(state="normal")
        self._host_entry.configure(state="disabled")
        self._video_label.configure(text="映像を待っています...")
        self._status_var.set(f"接続しました: {stream.url}")

    def _on_disconnect(self) -> None:
        self._stop_stream()
        self._status_var.set("切断しました。")

    def _on_capture(self) -> None:
        try:
            host = api.normalize_host(self._host_var.get())
        except ApiError as exc:
            messagebox.showerror("入力エラー", str(exc), parent=self)
            return

        flash = self._flash_var.get()
        self._run(
            lambda: api.capture(host, flash),
            self._on_captured,
            "撮影しています...",
        )

    def _on_captured(self, name: str) -> None:
        self._status_var.set(f"SD カードに保存しました: {name}")
        self._on_refresh_photos()

    def _on_status(self) -> None:
        try:
            host = api.normalize_host(self._host_var.get())
        except ApiError as exc:
            messagebox.showerror("入力エラー", str(exc), parent=self)
            return

        self._run(
            lambda: api.fetch_status(host),
            lambda status: self._status_var.set(api.describe_status(status)),
            "状態を取得しています...",
        )

    def _on_refresh_photos(self) -> None:
        try:
            host = api.normalize_host(self._host_var.get())
        except ApiError as exc:
            messagebox.showerror("入力エラー", str(exc), parent=self)
            return

        self._run(
            lambda: api.list_photos(host),
            self._on_photos_listed,
            "写真一覧を取得しています...",
        )

    def _on_photos_listed(self, names: list[str]) -> None:
        self._photo_list.delete(0, tk.END)
        for name in names:
            self._photo_list.insert(tk.END, name)
        self._status_var.set(f"写真 {len(names)} 件を取得しました。")

    def _on_show_photo(self) -> None:
        if self._busy:
            return

        selection = self._photo_list.curselection()
        if not selection:
            messagebox.showinfo("表示", "写真を選んでください。", parent=self)
            return

        name = self._photo_list.get(selection[0])
        try:
            host = api.normalize_host(self._host_var.get())
        except ApiError as exc:
            messagebox.showerror("入力エラー", str(exc), parent=self)
            return

        self._run(
            lambda: api.fetch_photo(host, name),
            lambda data: self._open_preview(name, data),
            f"写真を取得しています: {name}",
        )

    def _open_preview(self, name: str, data: bytes) -> None:
        try:
            PhotoPreview(self, name, data, DOWNLOAD_DIR)
        except (OSError, ValueError) as exc:
            messagebox.showerror(
                "表示エラー", f"写真を表示できませんでした: {exc}", parent=self
            )
            return
        self._status_var.set(f"表示しました: {name}")

    # ------------------------------------------------------------------
    # 定期処理（映像更新とスレッド結果の受け取り）
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        self._drain_queue()

        stream = self._stream
        if stream is not None:
            error = stream.take_error()
            if error is not None:
                self._stop_stream()
                self._status_var.set("エラーで切断しました。")
                messagebox.showerror("映像エラー", error, parent=self)
            else:
                frame = stream.latest_frame()
                if frame is not None:
                    self._show_frame(frame)

        self._tick_id = self.after(_INTERVAL_MS, self._tick)

    def _show_frame(self, frame: np.ndarray) -> None:
        image = fit_image(
            frame_to_image(frame),
            self._video_label.winfo_width(),
            self._video_label.winfo_height(),
        )
        photo = ImageTk.PhotoImage(image)
        self._photo = photo
        self._video_label.configure(image=photo, text="")

    # ------------------------------------------------------------------
    # 後始末
    # ------------------------------------------------------------------
    def _stop_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.stop()

        self._photo = None
        self._video_label.configure(image="", text="未接続")

        self._connect_button.configure(state="disabled" if self._busy else "normal")
        self._disconnect_button.configure(state="disabled")
        self._host_entry.configure(state="normal")

    def shutdown(self) -> None:
        """ウィンドウを閉じるときに呼ぶ。定期処理と接続を確実に止める。"""
        if self._tick_id is not None:
            self.after_cancel(self._tick_id)
            self._tick_id = None
        self._stop_stream()
