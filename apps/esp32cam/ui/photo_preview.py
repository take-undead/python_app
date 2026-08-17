"""SD カードから取得した写真を表示するプレビューウィンドウ。"""

from __future__ import annotations

import io
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from ui.imaging import fit_image


class PhotoPreview(tk.Toplevel):
    """写真 1 枚を表示し、PC に保存できるウィンドウ。"""

    def __init__(self, master: tk.Misc, name: str, data: bytes, save_dir: Path) -> None:
        super().__init__(master)

        self._name = name
        self._data = data
        self._save_dir = save_dir
        self._image = Image.open(io.BytesIO(data))
        self._image.load()  # BytesIO を閉じても使えるよう先に読み込む
        self._photo: ImageTk.PhotoImage | None = None  # GC 防止のため保持
        self._shown_size = (0, 0)

        self.title(f"{name}  ({self._image.width}×{self._image.height})")
        self.geometry("900x700")
        self.minsize(360, 280)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._image_label = tk.Label(self, background="#202020")
        self._image_label.grid(row=0, column=0, sticky="nsew")

        footer = ttk.Frame(self, padding=6)
        footer.grid(row=1, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)

        ttk.Label(footer, text=name).grid(row=0, column=0, sticky="w")
        ttk.Button(footer, text="PC に保存", command=self._on_save, width=12).grid(
            row=0, column=1
        )
        ttk.Button(footer, text="閉じる", command=self.destroy, width=10).grid(
            row=0, column=2, padx=(6, 0)
        )

        self._image_label.bind("<Configure>", self._on_resize)

    # ------------------------------------------------------------------
    # 表示
    # ------------------------------------------------------------------
    def _on_resize(self, event: tk.Event) -> None:
        size = (event.width, event.height)
        # 画像を差し替えると再び <Configure> が飛ぶため、同じサイズなら何もしない
        if size == self._shown_size:
            return
        self._shown_size = size
        self._render(*size)

    def _render(self, width: int, height: int) -> None:
        photo = ImageTk.PhotoImage(fit_image(self._image, width, height))
        self._photo = photo
        self._image_label.configure(image=photo)

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------
    def _on_save(self) -> None:
        self._save_dir.mkdir(parents=True, exist_ok=True)

        path = filedialog.asksaveasfilename(
            parent=self,
            title="写真を保存",
            initialdir=str(self._save_dir),
            initialfile=self._name.replace("/", "_"),
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
