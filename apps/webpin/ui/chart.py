"""ピン値の時系列を描くグラフウィジェット。

matplotlib を使わず tk.Canvas に直接描く（依存を増やさないため）。
再描画のたびに全アイテムを消して描き直す、単純な作りにしている。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from logic.series import SeriesStore

# 目盛りラベルなどのために確保する余白（左, 上, 右, 下）
_MARGIN = (56, 12, 12, 28)

# ピンごとに順番に割り当てる線の色
_COLORS = (
    "#1f77b4",
    "#d62728",
    "#2ca02c",
    "#ff7f0e",
    "#9467bd",
    "#17becf",
    "#8c564b",
    "#e377c2",
)

_BACKGROUND = "#ffffff"
_GRID = "#e0e0e0"
_AXIS = "#808080"
_TEXT = "#404040"

# 目盛りの本数
_Y_TICKS = 5
_X_TICKS = 5

_FONT = ("Meiryo UI", 8)


def color_for(index: int) -> str:
    """ピンの並び順に対応する線の色を返す。"""
    return _COLORS[index % len(_COLORS)]


class ChartFrame(ttk.Frame):
    """SeriesStore の内容を折れ線で描く。"""

    def __init__(self, master: tk.Misc, store: SeriesStore) -> None:
        super().__init__(master)

        self._store = store
        self._visible: set[str] = set()
        self._window_seconds: float | None = 30.0  # None なら全期間

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._canvas = tk.Canvas(
            self, background=_BACKGROUND, highlightthickness=1, highlightbackground=_AXIS
        )
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._canvas.bind("<Configure>", lambda _event: self.redraw())

    # ------------------------------------------------------------------
    # 表示設定
    # ------------------------------------------------------------------
    def set_visible_pins(self, pins: set[str]) -> None:
        """描画するピンを指定する。"""
        self._visible = set(pins)

    def set_window_seconds(self, seconds: float | None) -> None:
        """直近何秒を表示するかを指定する。None なら全期間。"""
        self._window_seconds = seconds

    # ------------------------------------------------------------------
    # 描画
    # ------------------------------------------------------------------
    def redraw(self) -> None:
        canvas = self._canvas
        canvas.delete("all")

        width = canvas.winfo_width()
        height = canvas.winfo_height()
        left, top, right, bottom = _MARGIN
        plot_width = width - left - right
        plot_height = height - top - bottom
        if plot_width <= 10 or plot_height <= 10:
            return  # まだレイアウトが確定していない

        pins = [pin for pin in self._store.pins() if pin in self._visible]
        if not pins:
            canvas.create_text(
                width / 2,
                height / 2,
                text="表示するデータがありません。",
                fill=_TEXT,
                font=("Meiryo UI", 10),
            )
            return

        x_min, x_max = self._x_range()
        y_min, y_max = self._store.value_range(pins)
        x_span = x_max - x_min or 1.0
        y_span = y_max - y_min or 1.0

        def to_x(seconds: float) -> float:
            return left + (seconds - x_min) / x_span * plot_width

        def to_y(value: float) -> float:
            return top + plot_height - (value - y_min) / y_span * plot_height

        self._draw_grid(
            left, top, plot_width, plot_height, x_min, x_span, y_min, y_span
        )

        all_pins = self._store.pins()
        for pin in pins:
            points = [
                (seconds, value)
                for seconds, value in self._store.points(pin)
                if seconds >= x_min
            ]
            if len(points) < 2:
                continue
            coordinates: list[float] = []
            for seconds, value in points:
                coordinates.extend((to_x(seconds), to_y(value)))
            canvas.create_line(
                *coordinates,
                fill=color_for(all_pins.index(pin)),
                width=1.5,
                joinstyle="round",
            )

    def _x_range(self) -> tuple[float, float]:
        start, end = self._store.time_range()
        window = self._window_seconds
        if window is None:
            return (start, max(end, start + 1.0))
        return (max(start, end - window), max(end, start + 1.0))

    def _draw_grid(
        self,
        left: int,
        top: int,
        plot_width: int,
        plot_height: int,
        x_min: float,
        x_span: float,
        y_min: float,
        y_span: float,
    ) -> None:
        canvas = self._canvas

        for index in range(_Y_TICKS + 1):
            ratio = index / _Y_TICKS
            y = top + plot_height - ratio * plot_height
            canvas.create_line(left, y, left + plot_width, y, fill=_GRID)
            canvas.create_text(
                left - 6,
                y,
                text=_format_number(y_min + ratio * y_span),
                anchor="e",
                fill=_TEXT,
                font=_FONT,
            )

        for index in range(_X_TICKS + 1):
            ratio = index / _X_TICKS
            x = left + ratio * plot_width
            canvas.create_line(x, top, x, top + plot_height, fill=_GRID)
            canvas.create_text(
                x,
                top + plot_height + 6,
                text=f"{x_min + ratio * x_span:.1f}s",
                anchor="n",
                fill=_TEXT,
                font=_FONT,
            )

        # 軸（左辺と下辺）を濃い色で上書きする
        canvas.create_line(left, top, left, top + plot_height, fill=_AXIS)
        canvas.create_line(
            left, top + plot_height, left + plot_width, top + plot_height, fill=_AXIS
        )


def _format_number(value: float) -> str:
    """目盛りラベル用に、桁数を抑えて数値を文字列にする。"""
    if abs(value) >= 1000 or (value != 0 and abs(value) < 0.01):
        return f"{value:.3g}"
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"
