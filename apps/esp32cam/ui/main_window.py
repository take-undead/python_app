"""メイン画面。ESP32-CAM 6 台の映像表示・撮影・SD カード内の写真閲覧を行う。

映像は 6 台分を同時に表示し、撮影・写真一覧・状態取得は
タイルをクリックして選んだ 1 台（＋「全カメラ撮影」）に対して行う。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

from logic import api, settings
from logic.api import ApiError
from logic.stream import StreamError
from ui.camera_tile import CameraTile
from ui.photo_preview import PhotoPreview

# 画面更新の間隔（ミリ秒）。6 台分をまとめて更新する
_INTERVAL_MS = 40

# タイルを並べる列数（6 台を 3 列 × 2 行に配置する）
_COLUMNS = 3

# PC に写真を保存するときの既定フォルダ
DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"


class MainWindow(ttk.Frame):
    """ESP32-CAM ビューアのメイン画面。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=8)

        self._queue: queue.Queue[tuple[Callable[[Any], None], Any]] = queue.Queue()
        self._tick_id: str | None = None
        self._busy = False

        # 接続結果は 6 台分まとめて 1 つのダイアログにする
        self._pending_connects = 0
        self._connect_errors: list[str] = []

        # 全カメラ撮影の集計
        self._pending_captures = 0
        self._capture_errors: list[str] = []
        self._capture_done = 0

        self._tiles: list[CameraTile] = []
        self._selected: CameraTile | None = None

        self._flash_var = tk.BooleanVar(value=False)
        self._target_var = tk.StringVar(value="")
        self._status_var = tk.StringVar(
            value="IP アドレスを確認して「全カメラ接続」を押してください。"
        )

        self._build_widgets()
        self._select_tile(self._tiles[0])
        self._tick()

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._build_toolbar()

        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew", pady=8)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0)
        body.rowconfigure(0, weight=1)

        self._build_tiles(body)
        self._build_photo_panel(body)

        status = ttk.Label(self, textvariable=self._status_var, anchor="w")
        status.grid(row=2, column=0, sticky="ew")

    def _build_toolbar(self) -> None:
        control = ttk.Frame(self)
        control.grid(row=0, column=0, sticky="ew")

        ttk.Button(
            control, text="全カメラ接続", command=self._on_connect_all, width=13
        ).grid(row=0, column=0, padx=(0, 4))

        ttk.Button(
            control, text="全カメラ切断", command=self._on_disconnect_all, width=13
        ).grid(row=0, column=1, padx=(0, 12))

        self._flash_check = ttk.Checkbutton(
            control, text="フラッシュ", variable=self._flash_var
        )
        self._flash_check.grid(row=0, column=2, padx=(0, 8))

        self._capture_button = ttk.Button(
            control, text="撮影", command=self._on_capture, width=8
        )
        self._capture_button.grid(row=0, column=3, padx=(0, 4))

        self._capture_all_button = ttk.Button(
            control, text="全カメラ撮影", command=self._on_capture_all, width=13
        )
        self._capture_all_button.grid(row=0, column=4, padx=(0, 4))

        self._status_button = ttk.Button(
            control, text="状態", command=self._on_status, width=8
        )
        self._status_button.grid(row=0, column=5)

        # 右端に余白を寄せて、操作部を左詰めにする
        control.columnconfigure(6, weight=1)

    def _build_tiles(self, master: tk.Misc) -> None:
        """カメラ 6 台分のタイルを格子状に並べる。"""
        tile_grid = ttk.Frame(master)
        tile_grid.grid(row=0, column=0, sticky="nsew")

        hosts = settings.load_hosts()
        rows = -(-len(hosts) // _COLUMNS)  # 切り上げ

        for column in range(_COLUMNS):
            tile_grid.columnconfigure(column, weight=1, uniform="camera")
        for row in range(rows):
            tile_grid.rowconfigure(row, weight=1, uniform="camera")

        for index, host in enumerate(hosts):
            tile = CameraTile(
                tile_grid,
                index=index,
                host=host,
                task_queue=self._queue,
                on_select=self._select_tile,
                on_connect_request=self._connect_tile,
                on_connect_result=self._on_connect_result,
                on_status=self._status_var.set,
            )
            tile.grid(
                row=index // _COLUMNS,
                column=index % _COLUMNS,
                sticky="nsew",
                padx=2,
                pady=2,
            )
            self._tiles.append(tile)

    def _build_photo_panel(self, master: tk.Misc) -> None:
        """SD カード内の写真を扱う右側のパネルを作る。対象は選択中のカメラ。"""
        panel = ttk.Frame(master, padding=(8, 0, 0, 0))
        panel.grid(row=0, column=1, sticky="ns")
        panel.rowconfigure(2, weight=1)

        ttk.Label(panel, text="SD カード内の写真").grid(row=0, column=0, sticky="w")
        ttk.Label(panel, textvariable=self._target_var, foreground="#1a73e8").grid(
            row=1, column=0, sticky="w"
        )

        list_frame = ttk.Frame(panel)
        list_frame.grid(row=2, column=0, sticky="nsew", pady=4)
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
        buttons.grid(row=3, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)

        self._refresh_button = ttk.Button(
            buttons, text="一覧更新", command=self._on_refresh_photos
        )
        self._refresh_button.grid(row=0, column=0, sticky="ew", padx=(0, 2))

        self._show_button = ttk.Button(buttons, text="表示", command=self._on_show_photo)
        self._show_button.grid(row=0, column=1, sticky="ew", padx=(2, 0))

    # ------------------------------------------------------------------
    # 操作対象のカメラ
    # ------------------------------------------------------------------
    def _select_tile(self, tile: CameraTile) -> None:
        if self._selected is tile:
            return

        self._selected = tile
        for other in self._tiles:
            other.set_selected(other is tile)

        self._target_var.set(f"対象: {tile.label}")
        # 別のカメラの一覧をそのまま残すと取り違えるため消す
        self._photo_list.delete(0, tk.END)

    def _selected_target(self) -> tuple[CameraTile, str] | None:
        """選択中のタイルと IP を返す。不正なときはダイアログを出して None を返す。"""
        tile = self._selected
        if tile is None:
            return None

        try:
            return tile, tile.host()
        except ApiError as exc:
            messagebox.showerror("入力エラー", f"{tile.label}: {exc}", parent=self)
            return None

    # ------------------------------------------------------------------
    # 非同期処理（通信は必ず別スレッドで行い、結果を after 経由で受け取る）
    # ------------------------------------------------------------------
    def _run(
        self,
        task: Callable[[], Any],
        on_success: Callable[[Any], None],
        busy_message: str,
        on_error: Callable[[Exception], None] | None = None,
    ) -> bool:
        """別スレッドで task を実行する。他の処理中で開始できなければ False。"""
        if self._busy:
            return False

        self._set_busy(True)
        self._status_var.set(busy_message)
        failed = on_error or self._on_task_error

        def worker() -> None:
            try:
                result = task()
            except (ApiError, StreamError) as exc:
                self._queue.put((self._finish, (failed, exc)))
            else:
                self._queue.put((self._finish, (on_success, result)))

        threading.Thread(target=worker, name="esp32cam-task", daemon=True).start()
        return True

    def _finish(self, payload: tuple[Callable[[Any], None], Any]) -> None:
        callback, result = payload
        self._set_busy(False)
        callback(result)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self._capture_button,
            self._capture_all_button,
            self._status_button,
            self._refresh_button,
            self._show_button,
        ):
            button.configure(state=state)

    def _on_task_error(self, exc: Exception) -> None:
        self._status_var.set("エラーが発生しました。")
        messagebox.showerror("通信エラー", str(exc), parent=self)

    def _drain_queue(self) -> None:
        while True:
            try:
                callback, result = self._queue.get_nowait()
            except queue.Empty:
                return
            callback(result)

    # ------------------------------------------------------------------
    # 接続・切断
    # ------------------------------------------------------------------
    def _connect_tile(self, tile: CameraTile) -> None:
        if tile.is_connected or tile.is_connecting:
            return

        # 結果が返るまでの台数を数え、全部出そろってからまとめて報告する
        self._pending_connects += 1
        tile.connect()

    def _on_connect_all(self) -> None:
        targets = [
            tile
            for tile in self._tiles
            if not tile.is_connected and not tile.is_connecting
        ]
        if not targets:
            self._status_var.set("すべてのカメラが接続済みです。")
            return

        self._status_var.set(f"{len(targets)} 台に接続しています...")
        for tile in targets:
            self._connect_tile(tile)

    def _on_connect_result(self, tile: CameraTile, error: str | None) -> None:
        self._pending_connects = max(self._pending_connects - 1, 0)
        if error is not None:
            self._connect_errors.append(f"{tile.label}（{tile.host_var.get()}）\n{error}")

        if self._pending_connects > 0:
            return

        errors = self._connect_errors
        self._connect_errors = []

        self._save_hosts()
        connected = sum(1 for one in self._tiles if one.is_connected)
        self._status_var.set(f"{connected} / {len(self._tiles)} 台を表示中です。")

        if errors:
            messagebox.showerror("接続エラー", "\n\n".join(errors), parent=self)

    def _on_disconnect_all(self) -> None:
        for tile in self._tiles:
            tile.disconnect()
        self._save_hosts()
        self._status_var.set("すべて切断しました。")

    def _save_hosts(self) -> None:
        settings.save_hosts([tile.host_var.get() for tile in self._tiles])

    # ------------------------------------------------------------------
    # 撮影・状態
    # ------------------------------------------------------------------
    def _on_capture(self) -> None:
        target = self._selected_target()
        if target is None:
            return

        tile, host = target
        flash = self._flash_var.get()
        self._run(
            lambda: api.capture(host, flash),
            self._on_captured,
            f"{tile.label} で撮影しています...",
        )

    def _on_captured(self, name: str) -> None:
        self._status_var.set(f"SD カードに保存しました: {name}")
        self._on_refresh_photos()

    def _on_capture_all(self) -> None:
        if self._busy:
            return

        targets: list[tuple[CameraTile, str]] = []
        errors: list[str] = []
        for tile in self._tiles:
            try:
                targets.append((tile, tile.host()))
            except ApiError as exc:
                errors.append(f"{tile.label}: {exc}")

        if not targets:
            messagebox.showerror(
                "入力エラー", "撮影できるカメラがありません。", parent=self
            )
            return

        flash = self._flash_var.get()
        self._pending_captures = len(targets)
        self._capture_errors = errors
        self._capture_done = 0

        self._set_busy(True)
        self._status_var.set(f"{len(targets)} 台で撮影しています...")

        for tile, host in targets:
            def worker(tile: CameraTile = tile, host: str = host) -> None:
                try:
                    api.capture(host, flash)
                except ApiError as exc:
                    self._queue.put((self._on_capture_all_result, (tile, str(exc))))
                else:
                    self._queue.put((self._on_capture_all_result, (tile, None)))

            threading.Thread(
                target=worker, name=f"esp32cam-capture-{tile.index}", daemon=True
            ).start()

    def _on_capture_all_result(self, payload: tuple[CameraTile, str | None]) -> None:
        tile, error = payload
        if error is None:
            self._capture_done += 1
        else:
            self._capture_errors.append(f"{tile.label}\n{error}")

        self._pending_captures = max(self._pending_captures - 1, 0)
        if self._pending_captures > 0:
            return

        errors = self._capture_errors
        self._capture_errors = []

        self._set_busy(False)
        self._status_var.set(
            f"{self._capture_done} / {len(self._tiles)} 台で撮影しました。"
        )
        if errors:
            messagebox.showerror("撮影エラー", "\n\n".join(errors), parent=self)

    def _on_status(self) -> None:
        target = self._selected_target()
        if target is None:
            return

        tile, host = target
        self._run(
            lambda: api.fetch_status(host),
            lambda status: self._status_var.set(
                f"{tile.label}  {api.describe_status(status)}"
            ),
            f"{tile.label} の状態を取得しています...",
        )

    # ------------------------------------------------------------------
    # 写真一覧
    # ------------------------------------------------------------------
    def _on_refresh_photos(self) -> None:
        target = self._selected_target()
        if target is None:
            return

        tile, host = target
        self._run(
            lambda: api.list_photos(host),
            self._on_photos_listed,
            f"{tile.label} の写真一覧を取得しています...",
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

        target = self._selected_target()
        if target is None:
            return

        _tile, host = target
        names = list(self._photo_list.get(0, tk.END))
        index = selection[0]
        self._run(
            lambda: api.fetch_photo(host, names[index]),
            lambda data: self._open_preview(names, index, data, host),
            f"写真を取得しています: {names[index]}",
        )

    def _open_preview(
        self, names: list[str], index: int, data: bytes, host: str
    ) -> None:
        try:
            PhotoPreview(
                self,
                names,
                index,
                data,
                DOWNLOAD_DIR,
                # 前後の写真の取得は通信を伴うため、プレビューから依頼を受けて行う
                on_request=lambda preview, position: self._request_photo(
                    preview, position, host
                ),
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror(
                "表示エラー", f"写真を表示できませんでした: {exc}", parent=self
            )
            return
        self._status_var.set(f"表示しました: {names[index]}")

    def _request_photo(self, preview: PhotoPreview, index: int, host: str) -> None:
        """プレビューから依頼された写真を取得して渡す。"""
        name = preview.names[index]
        started = self._run(
            lambda: api.fetch_photo(host, name),
            lambda data: self._deliver_photo(preview, index, data),
            f"写真を取得しています: {name}",
            on_error=lambda exc: self._photo_request_failed(preview, exc),
        )
        if not started:
            preview.load_failed()

    def _deliver_photo(self, preview: PhotoPreview, index: int, data: bytes) -> None:
        # 取得中にプレビューが閉じられていることがある
        if not preview.winfo_exists():
            return
        preview.show(index, data)
        self._status_var.set(f"表示しました: {preview.names[index]}")

    def _photo_request_failed(self, preview: PhotoPreview, exc: Exception) -> None:
        if preview.winfo_exists():
            preview.load_failed()
        self._on_task_error(exc)

    # ------------------------------------------------------------------
    # 定期処理（映像更新とスレッド結果の受け取り）
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        self._drain_queue()
        for tile in self._tiles:
            tile.poll()
        self._tick_id = self.after(_INTERVAL_MS, self._tick)

    # ------------------------------------------------------------------
    # 後始末
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """ウィンドウを閉じるときに呼ぶ。定期処理と全接続を確実に止める。"""
        if self._tick_id is not None:
            self.after_cancel(self._tick_id)
            self._tick_id = None

        self._save_hosts()
        for tile in self._tiles:
            tile.shutdown()
