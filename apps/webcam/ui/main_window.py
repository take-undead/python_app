"""メイン画面。カメラ映像の表示と静止画の保存を行う。"""

from __future__ import annotations

import datetime as dt
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

from logic.camera import CameraCapture, CameraError, list_devices, save_frame

# 画面更新の間隔（ミリ秒）。約 30 fps
_INTERVAL_MS = 33

# デバイス検出の完了を待つポーリング間隔（ミリ秒）
_POLL_MS = 200

# 検出を実行するまでは、この範囲のインデックスを候補として並べる
_DEFAULT_DEVICE_INDEXES = (0, 1, 2, 3)

# 撮影した静止画の保存先（カレントディレクトリに依存させない）
CAPTURE_DIR = Path(__file__).resolve().parent.parent / "captures"


class MainWindow(ttk.Frame):
    """Web カメラビューアのメイン画面。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=8)

        self._camera: CameraCapture | None = None
        self._current_frame: np.ndarray | None = None
        self._photo: ImageTk.PhotoImage | None = None  # GC 防止のため参照を保持
        self._after_id: str | None = None
        self._detect_thread: threading.Thread | None = None
        self._detect_result: list[int] = []

        self._device_var = tk.StringVar(value="0")
        self._status_var = tk.StringVar(value="カメラを選んで「開始」を押してください。")

        self._build_widgets()

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        control = ttk.Frame(self)
        control.grid(row=0, column=0, sticky="ew")

        ttk.Label(control, text="カメラ:").grid(row=0, column=0, padx=(0, 4))

        self._device_combo = ttk.Combobox(
            control,
            textvariable=self._device_var,
            values=[str(index) for index in _DEFAULT_DEVICE_INDEXES],
            state="readonly",
            width=6,
        )
        self._device_combo.grid(row=0, column=1, padx=(0, 4))

        self._detect_button = ttk.Button(
            control, text="検出", command=self._on_detect, width=8
        )
        self._detect_button.grid(row=0, column=2, padx=(0, 12))

        self._start_button = ttk.Button(
            control, text="開始", command=self._on_start, width=8
        )
        self._start_button.grid(row=0, column=3, padx=(0, 4))

        self._stop_button = ttk.Button(
            control, text="停止", command=self._on_stop, width=8, state="disabled"
        )
        self._stop_button.grid(row=0, column=4, padx=(0, 12))

        self._snapshot_button = ttk.Button(
            control, text="撮影", command=self._on_snapshot, width=8, state="disabled"
        )
        self._snapshot_button.grid(row=0, column=5)

        # 右端に余白を寄せて、ボタン群を左詰めにする
        control.columnconfigure(6, weight=1)

        self._video_label = tk.Label(
            self,
            background="#202020",
            foreground="#f0f0f0",
            font=("Meiryo UI", 11),
            text="停止中",
        )
        self._video_label.grid(row=1, column=0, sticky="nsew", pady=8)

        status = ttk.Label(self, textvariable=self._status_var, anchor="w")
        status.grid(row=2, column=0, sticky="ew")

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        if self._camera is not None:
            return

        try:
            device_index = int(self._device_var.get())
        except ValueError:
            messagebox.showerror(
                "入力エラー", "カメラ番号が正しくありません。", parent=self
            )
            return

        camera = CameraCapture(device_index)
        try:
            camera.start()
        except CameraError as exc:
            messagebox.showerror("カメラエラー", str(exc), parent=self)
            return

        self._camera = camera
        self._start_button.configure(state="disabled")
        self._detect_button.configure(state="disabled")
        self._device_combo.configure(state="disabled")
        self._stop_button.configure(state="normal")
        self._snapshot_button.configure(state="normal")
        self._status_var.set(f"カメラ {device_index} を表示中です。")
        self._update_frame()

    def _on_stop(self) -> None:
        self._stop_camera()
        self._status_var.set("停止しました。")

    def _on_snapshot(self) -> None:
        frame = self._current_frame
        if frame is None:
            messagebox.showinfo("撮影", "まだ映像を取得できていません。", parent=self)
            return

        path = CAPTURE_DIR / f"{dt.datetime.now():%Y%m%d_%H%M%S}.png"
        try:
            CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
            save_frame(frame, path)
        except (CameraError, OSError) as exc:
            messagebox.showerror("保存エラー", str(exc), parent=self)
            return

        self._status_var.set(f"保存しました: {path}")

    def _on_detect(self) -> None:
        """接続されているカメラを調べる。時間がかかるので別スレッドで実行する。"""
        if self._detect_thread is not None:
            return

        self._detect_button.configure(state="disabled")
        self._status_var.set("カメラを検出しています...")

        def worker() -> None:
            self._detect_result = list_devices()

        self._detect_thread = threading.Thread(
            target=worker, name="camera-detect", daemon=True
        )
        self._detect_thread.start()
        self.after(_POLL_MS, self._poll_detect)

    def _poll_detect(self) -> None:
        thread = self._detect_thread
        if thread is not None and thread.is_alive():
            self.after(_POLL_MS, self._poll_detect)
            return

        self._detect_thread = None
        devices = self._detect_result

        if devices:
            self._device_combo.configure(values=[str(index) for index in devices])
            if self._device_var.get() not in {str(index) for index in devices}:
                self._device_var.set(str(devices[0]))
            self._status_var.set(
                "利用できるカメラ: " + ", ".join(str(index) for index in devices)
            )
        else:
            self._status_var.set("利用できるカメラが見つかりませんでした。")

        if self._camera is None:
            self._detect_button.configure(state="normal")

    # ------------------------------------------------------------------
    # 映像の更新
    # ------------------------------------------------------------------
    def _update_frame(self) -> None:
        self._after_id = None

        camera = self._camera
        if camera is None:
            return

        error = camera.take_error()
        if error is not None:
            self._stop_camera()
            self._status_var.set("エラーで停止しました。")
            messagebox.showerror("カメラエラー", error, parent=self)
            return

        frame = camera.latest_frame()
        if frame is not None:
            self._current_frame = frame
            self._show_frame(frame)

        self._after_id = self.after(_INTERVAL_MS, self._update_frame)

    def _show_frame(self, frame: np.ndarray) -> None:
        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

        # 表示領域に収まるよう、縦横比を保ったまま拡大縮小する
        area_width = self._video_label.winfo_width()
        area_height = self._video_label.winfo_height()
        if area_width > 1 and area_height > 1:
            scale = min(area_width / image.width, area_height / image.height)
            size = (
                max(int(image.width * scale), 1),
                max(int(image.height * scale), 1),
            )
            image = image.resize(size, Image.LANCZOS)

        photo = ImageTk.PhotoImage(image)
        self._photo = photo
        self._video_label.configure(image=photo, text="")

    # ------------------------------------------------------------------
    # 後始末
    # ------------------------------------------------------------------
    def _stop_camera(self) -> None:
        if self._after_id is not None:
            self.after_cancel(self._after_id)
            self._after_id = None

        camera = self._camera
        self._camera = None
        if camera is not None:
            camera.stop()

        self._current_frame = None
        self._photo = None
        self._video_label.configure(image="", text="停止中")

        self._start_button.configure(state="normal")
        self._detect_button.configure(state="normal")
        self._device_combo.configure(state="readonly")
        self._stop_button.configure(state="disabled")
        self._snapshot_button.configure(state="disabled")

    def shutdown(self) -> None:
        """ウィンドウを閉じるときに呼ぶ。カメラを確実に解放する。"""
        self._stop_camera()
