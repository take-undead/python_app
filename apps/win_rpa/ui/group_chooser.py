"""手順を移す先のグループを、今あるものから選ぶ小窓。

グループ名を打ち直させると、1 文字違いの別グループが黙って増える。
移動先は必ず一覧から選ばせ、新しく作るときだけ名前を打たせる。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable


class GroupChooser(tk.Toplevel):
    """今あるグループの一覧から 1 つ選ぶ。"""

    def __init__(
        self,
        master: tk.Misc,
        names: list[str],
        on_chosen: Callable[[str], None],
    ) -> None:
        super().__init__(master)
        self._on_chosen = on_chosen
        self._names = names

        self.title("別のグループへ移す")
        self.geometry("360x320")
        self.minsize(300, 240)
        self.transient(master)

        self._new_var = tk.StringVar()

        self._build_widgets()
        self.bind("<Escape>", lambda _e: self.destroy())

    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        frame = ttk.Frame(self, padding=10)
        frame.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        ttk.Label(frame, text="移す先のグループ").grid(row=0, column=0, sticky="w")

        self._list = tk.Listbox(frame, font=("Meiryo UI", 10), activestyle="none")
        self._list.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(4, 8))
        self._list.bind("<Double-Button-1>", lambda _e: self._choose_existing())
        for name in self._names:
            self._list.insert(tk.END, f" {name}")
        if self._names:
            self._list.selection_set(0)

        ttk.Button(frame, text="ここへ移す", command=self._choose_existing).grid(
            row=2, column=0, columnspan=2, sticky="ew"
        )

        ttk.Separator(frame, orient="horizontal").grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=10
        )

        ttk.Label(frame, text="新しいグループを作る").grid(
            row=4, column=0, columnspan=2, sticky="w"
        )
        entry = ttk.Entry(frame, textvariable=self._new_var)
        entry.grid(row=5, column=0, sticky="ew", pady=(4, 0))
        entry.bind("<Return>", lambda _e: self._choose_new())
        ttk.Button(frame, text="作って移す", command=self._choose_new, width=12).grid(
            row=5, column=1, sticky="e", padx=(6, 0), pady=(4, 0)
        )

    # ------------------------------------------------------------------
    def _finish(self, name: str) -> None:
        self.destroy()
        self._on_chosen(name)

    def _choose_existing(self) -> None:
        selection = self._list.curselection()
        if not selection:
            return
        self._finish(self._names[selection[0]])

    def _choose_new(self) -> None:
        name = self._new_var.get().strip()
        if name:
            self._finish(name)
