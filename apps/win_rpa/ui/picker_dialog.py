"""要素を採取する小窓。

対象アプリのボタンにマウスを乗せて F8 を押してもらう。押す前に
カーソル下に何があるかを出しておくと、狙いが外れたまま採取するのを防げる。

キーの検出は GetAsyncKeyState の定期確認で行う。ホットキーの登録
（RegisterHotKey）だとメッセージループが要るうえ、他アプリと取り合いになる。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from logic.picker import (
    VK_ESCAPE,
    VK_F8,
    ElementRef,
    PickerError,
    capture_at_cursor,
    describe,
    is_identifiable,
    is_key_pressed,
    peek_at_cursor,
)

# カーソル下の下見を更新する間隔（ミリ秒）
_PEEK_MS = 200


class PickerDialog(tk.Toplevel):
    """マウス位置の要素を採取する小窓。"""

    def __init__(self, master: tk.Misc, on_picked: Callable[[ElementRef], None]) -> None:
        super().__init__(master)
        self._on_picked = on_picked
        self._picked: ElementRef | None = None
        self._tick_id: str | None = None
        self._waiting_release = False

        self.title("要素を選ぶ")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self._peek_var = tk.StringVar(value="（カーソルを対象に乗せてください）")
        self._build_widgets()
        self._place_at_corner()

        self._tick()

    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        frame = ttk.Frame(self, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)

        ttk.Label(
            frame,
            text="対象アプリのボタンや入力欄にマウスを乗せて F8 を押してください。",
            wraplength=380,
        ).grid(row=0, column=0, sticky="w")

        ttk.Separator(frame, orient="horizontal").grid(
            row=1, column=0, sticky="ew", pady=8
        )

        ttk.Label(frame, text="カーソルの下:").grid(row=2, column=0, sticky="w")
        ttk.Label(
            frame,
            textvariable=self._peek_var,
            font=("Meiryo UI", 11, "bold"),
            wraplength=380,
        ).grid(row=3, column=0, sticky="w", pady=(2, 8))

        ttk.Label(
            frame, text="Esc で取り消します。", foreground="#666666"
        ).grid(row=4, column=0, sticky="w")

        ttk.Button(frame, text="取り消す", command=self._cancel).grid(
            row=5, column=0, sticky="e", pady=(8, 0)
        )

    def _place_at_corner(self) -> None:
        """対象アプリを隠さないよう、画面の左上に寄せる。"""
        self.update_idletasks()
        self.geometry(f"+40+40")

    # ------------------------------------------------------------------
    def _tick(self) -> None:
        # キーを押しっぱなしにしても 1 回だけ拾う
        if self._waiting_release:
            if not (is_key_pressed(VK_F8) or is_key_pressed(VK_ESCAPE)):
                self._waiting_release = False
        elif is_key_pressed(VK_ESCAPE):
            self._waiting_release = True
            self._cancel()
            return
        elif is_key_pressed(VK_F8):
            self._waiting_release = True
            self._capture()
            return
        else:
            self._peek_var.set(peek_at_cursor())

        self._tick_id = self.after(_PEEK_MS, self._tick)

    def _capture(self) -> None:
        try:
            ref = capture_at_cursor()
        except PickerError as exc:
            self._peek_var.set(f"採取できませんでした: {exc}")
            self._tick_id = self.after(_PEEK_MS, self._tick)
            return

        if not is_identifiable(ref):
            self._peek_var.set(
                f"{describe(ref)} は名前も ID も持たないため、"
                "確実に見つけられません。別の場所を選んでください。"
            )
            self._tick_id = self.after(_PEEK_MS, self._tick)
            return

        self._picked = ref
        self._close()
        self._on_picked(ref)

    def _cancel(self) -> None:
        self._picked = None
        self._close()

    def _close(self) -> None:
        if self._tick_id is not None:
            self.after_cancel(self._tick_id)
            self._tick_id = None
        self.destroy()
