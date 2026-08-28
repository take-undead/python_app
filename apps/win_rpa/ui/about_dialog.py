"""「このソフトについて」の小窓。

ツールバー右端の版表示をクリックすると開く。何のためのソフトかと、
**まだ作成中である**ことを伝えるのが目的。

本文は Text に入れる。ラベルを並べるより長い文章を扱いやすく、
データの保存場所を選んで写せる（調べ物のときに要る）。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from logic import appinfo, storage

_BODY_FONT = ("Meiryo UI", 9)
_HEAD_FONT = ("Meiryo UI", 10, "bold")


class AboutDialog(tk.Toplevel):
    """このソフトについての説明を出す。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)

        self.title(f"{appinfo.APP_NAME} について")
        self.geometry("620x560")
        self.minsize(520, 420)
        self.transient(master)

        self._build_widgets()
        self.bind("<Escape>", lambda _e: self.destroy())

    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        frame = ttk.Frame(self, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        ttk.Label(
            frame, text=appinfo.APP_NAME, font=("Meiryo UI", 16, "bold")
        ).grid(row=0, column=0, sticky="w")

        ttk.Label(
            frame,
            text=appinfo.title_line(),
            font=("Meiryo UI", 10),
            foreground="#b7791f" if appinfo.IS_BETA else "#666666",
        ).grid(row=1, column=0, sticky="w", pady=(2, 10))

        self._body = tk.Text(
            frame, font=_BODY_FONT, wrap="word", state="disabled",
            relief="flat", background=self.cget("background"),
            highlightthickness=0, padx=0, pady=0,
        )
        self._body.grid(row=2, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(
            frame, orient="vertical", command=self._body.yview
        )
        scrollbar.grid(row=2, column=1, sticky="ns")
        self._body.configure(yscrollcommand=scrollbar.set)

        ttk.Button(frame, text="閉じる", command=self.destroy, width=12).grid(
            row=3, column=0, columnspan=2, sticky="e", pady=(12, 0)
        )

        self._fill()

    def _fill(self) -> None:
        text = self._body
        text.configure(state="normal")

        text.tag_configure("head", font=_HEAD_FONT, spacing1=10, spacing3=4)
        text.tag_configure("warn", foreground="#b7791f")
        text.tag_configure("warn_head", font=_HEAD_FONT, foreground="#b7791f",
                           spacing3=4)
        text.tag_configure("body", spacing3=2)
        text.tag_configure("path", font=("Consolas", 9), foreground="#444444")

        if appinfo.IS_BETA:
            text.insert(tk.END, "⚠ まだ作成中です\n", "warn_head")
            text.insert(tk.END, appinfo.BETA_NOTICE + "\n", "warn")

        text.insert(tk.END, "\n何をするソフトか\n", "head")
        text.insert(tk.END, appinfo.SUMMARY + "\n", "body")

        for heading, content in appinfo.SECTIONS:
            text.insert(tk.END, f"\n{heading}\n", "head")
            text.insert(tk.END, content + "\n", "body")

        # 調べ物のときに要るので、選んで写せる形で出す
        text.insert(tk.END, "\nデータの保存場所\n", "head")
        text.insert(tk.END, str(storage.app_dir()) + "\n", "path")
        text.insert(
            tk.END,
            "この下の scenarios（シナリオ）／work（CSV）／logs（実行ログ）に"
            "保存されます。\n",
            "body",
        )

        text.configure(state="disabled")
