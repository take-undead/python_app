"""SD カードから取得した写真を表示するプレビューウィンドウ。

左右端の矢印ボタン（と ← → キー）で、一覧の前後のファイルに切り替えられる。
切り替え先の取得は通信を伴うため、呼び出し元（MainWindow）に依頼し、
結果を show() で受け取る。このクラス自身は通信しない。
"""

from __future__ import annotations

import io
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from PIL import Image, ImageTk

from ui.imaging import fit_image


class PhotoPreview(tk.Toplevel):
    """写真 1 枚を表示し、前後のファイルに切り替えられるウィンドウ。"""

    def __init__(
        self,
        master: tk.Misc,
        names: list[str],
        index: int,
        data: bytes,
        save_dir: Path,
        on_request: Callable[["PhotoPreview", int], None],
    ) -> None:
        super().__init__(master)

        self.names = list(names)

        self._index = index
        self._data = data
        self._save_dir = save_dir
        self._on_request = on_request
        self._image = self._decode(data)
        self._photo: ImageTk.PhotoImage | None = None  # GC 防止のため保持
        self._shown_size = (0, 0)
        self._loading = False

        self._caption_var = tk.StringVar()

        self.geometry("900x700")
        self.minsize(420, 320)

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self._build_widgets()
        self._update_labels()

        self._image_label.bind("<Configure>", self._on_resize)
        self.bind("<Left>", lambda _event: self._go(-1))
        self.bind("<Right>", lambda _event: self._go(1))
        self.focus_set()

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        style = ttk.Style(self)
        style.configure("Nav.TButton", font=("Meiryo UI", 14))

        self._prev_button = ttk.Button(
            self, text="◀", width=3, style="Nav.TButton",
            command=lambda: self._go(-1),
        )
        self._prev_button.grid(row=0, column=0, sticky="ns")

        self._image_label = tk.Label(self, background="#202020")
        self._image_label.grid(row=0, column=1, sticky="nsew")

        self._next_button = ttk.Button(
            self, text="▶", width=3, style="Nav.TButton",
            command=lambda: self._go(1),
        )
        self._next_button.grid(row=0, column=2, sticky="ns")

        footer = ttk.Frame(self, padding=6)
        footer.grid(row=1, column=0, columnspan=3, sticky="ew")
        footer.columnconfigure(0, weight=1)

        ttk.Label(footer, textvariable=self._caption_var).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(footer, text="PC に保存", command=self._on_save, width=12).grid(
            row=0, column=1
        )
        ttk.Button(footer, text="閉じる", command=self.destroy, width=10).grid(
            row=0, column=2, padx=(6, 0)
        )

    # ------------------------------------------------------------------
    # 前後の写真への切り替え
    # ------------------------------------------------------------------
    def _go(self, step: int) -> None:
        if self._loading:
            return

        index = self._index + step
        if not 0 <= index < len(self.names):
            return

        self._loading = True
        self._update_buttons()
        self._caption_var.set(f"読み込んでいます: {self.names[index]}")
        self._on_request(self, index)

    def show(self, index: int, data: bytes) -> None:
        """依頼した写真が届いたときに呼ばれる。表示を差し替える。"""
        try:
            image = self._decode(data)
        except (OSError, ValueError) as exc:
            self.load_failed()
            messagebox.showerror(
                "表示エラー", f"写真を表示できませんでした: {exc}", parent=self
            )
            return

        self._index = index
        self._data = data
        self._image = image
        self._loading = False

        self._update_labels()
        self._render_current()

    def load_failed(self) -> None:
        """取得に失敗したときに呼ばれる。表示は変えず、操作だけ戻す。"""
        self._loading = False
        self._update_labels()

    def _update_labels(self) -> None:
        name = self.names[self._index]
        position = f"{self._index + 1} / {len(self.names)}"
        self.title(f"{name}  ({self._image.width}×{self._image.height})  {position}")
        self._caption_var.set(f"{name}   [{position}]")
        self._update_buttons()

    def _update_buttons(self) -> None:
        first = self._index <= 0
        last = self._index >= len(self.names) - 1
        self._prev_button.configure(
            state="disabled" if self._loading or first else "normal"
        )
        self._next_button.configure(
            state="disabled" if self._loading or last else "normal"
        )

    # ------------------------------------------------------------------
    # 表示
    # ------------------------------------------------------------------
    @staticmethod
    def _decode(data: bytes) -> Image.Image:
        image = Image.open(io.BytesIO(data))
        image.load()  # BytesIO を閉じても使えるよう先に読み込む
        return image

    def _on_resize(self, event: tk.Event) -> None:
        size = (event.width, event.height)
        # 画像を差し替えると再び <Configure> が飛ぶため、同じサイズなら何もしない
        if size == self._shown_size:
            return
        self._shown_size = size
        self._render(*size)

    def _render_current(self) -> None:
        width, height = self._shown_size
        if width <= 1 or height <= 1:
            width = self._image_label.winfo_width()
            height = self._image_label.winfo_height()
        self._render(width, height)

    def _render(self, width: int, height: int) -> None:
        photo = ImageTk.PhotoImage(fit_image(self._image, width, height))
        self._photo = photo
        self._image_label.configure(image=photo)

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------
    def _on_save(self) -> None:
        self._save_dir.mkdir(parents=True, exist_ok=True)

        name = self.names[self._index]
        path = filedialog.asksaveasfilename(
            parent=self,
            title="写真を保存",
            initialdir=str(self._save_dir),
            initialfile=name.replace("/", "_"),
            defaultextension=".jpg",
            filetypes=[("JPEG 画像", "*.jpg"), ("すべてのファイル", "*.*")],
        )
        if not path:
            return

        try:
            Path(path).write_bytes(self._data)
        except OSError as exc:
            messagebox.showerror("保存エラー", f"保存に失敗しました: {exc}", parent=self)
            return

        messagebox.showinfo("保存", f"保存しました:\n{path}", parent=self)
