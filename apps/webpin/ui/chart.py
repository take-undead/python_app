"""ピン値の時系列を描くグラフウィジェット。

matplotlib を使わず tk.Canvas に直接描く（依存を増やさないため）。
再描画のたびに全アイテムを消して描き直す、単純な作りにしている。

1 つの画面に複数並べて使う。縦軸の範囲はグラフごとに「自動」と
「最小・最大を指定」を切り替えられる。
"""

from __future__ import annotations

import datetime as dt
import tkinter as tk
from tkinter import messagebox, ttk

from logic.series import SeriesStore

# 目盛りラベルなどのために確保する余白（左, 上, 右, 下）。
# 右端は時刻ラベル（15:08:35）が半分はみ出す分を見込んで広めに取る
_MARGIN = (56, 12, 34, 28)

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

# 縦軸を動かすバーの長さ（ピクセル）
_SCALE_LENGTH = 96


def color_for(index: int) -> str:
    """ピンの並び順に対応する線の色を返す。"""
    return _COLORS[index % len(_COLORS)]


class ChartFrame(ttk.Frame):
    """SeriesStore の内容を折れ線で描く。

    縦軸は既定では表示中の値に合わせて自動で決まる。「自動」を外すと
    最小・最大の入力欄が有効になり、指定した範囲に固定される。
    """

    def __init__(self, master: tk.Misc, store: SeriesStore, title: str) -> None:
        super().__init__(master)

        self._store = store
        self._visible: set[str] = set()
        self._window_seconds: float | None = 30.0  # None なら全期間
        self._manual_range: tuple[float, float] | None = None  # None なら自動
        self._slider_domain: tuple[float, float] = (0.0, 1.0)  # バーで動かせる範囲
        self._validating = False  # エラー表示中に FocusOut で二重に出さないため
        self._syncing = False  # 数値欄とバーを揃える間、互いの反応を止める

        self._auto_var = tk.BooleanVar(value=True)
        self._min_var = tk.StringVar(value="")
        self._max_var = tk.StringVar(value="")

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._build_header(title)

        self._canvas = tk.Canvas(
            self, background=_BACKGROUND, highlightthickness=1, highlightbackground=_AXIS
        )
        self._canvas.grid(row=1, column=0, sticky="nsew")
        self._canvas.bind("<Configure>", lambda _event: self.redraw())

    def _build_header(self, title: str) -> None:
        """グラフ名と縦軸の指定欄を並べた見出し行。"""
        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 2))

        ttk.Label(header, text=title).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="縦軸:").grid(row=0, column=1, padx=(12, 2))
        ttk.Checkbutton(
            header, text="自動", variable=self._auto_var, command=self._on_auto_toggled
        ).grid(row=0, column=2)

        ttk.Label(header, text="最小").grid(row=0, column=3, padx=(8, 2))
        self._min_entry = ttk.Entry(
            header, textvariable=self._min_var, width=8, state="disabled"
        )
        self._min_entry.grid(row=0, column=4)
        self._min_scale = ttk.Scale(
            header,
            orient="horizontal",
            length=_SCALE_LENGTH,
            command=self._on_min_slider,
            state="disabled",
        )
        self._min_scale.grid(row=0, column=5, padx=(4, 0))

        ttk.Label(header, text="最大").grid(row=0, column=6, padx=(10, 2))
        self._max_entry = ttk.Entry(
            header, textvariable=self._max_var, width=8, state="disabled"
        )
        self._max_entry.grid(row=0, column=7)
        self._max_scale = ttk.Scale(
            header,
            orient="horizontal",
            length=_SCALE_LENGTH,
            command=self._on_max_slider,
            state="disabled",
        )
        self._max_scale.grid(row=0, column=8, padx=(4, 0))

        for entry in (self._min_entry, self._max_entry):
            entry.bind("<Return>", self._on_range_entered)
            entry.bind("<FocusOut>", self._on_range_entered)

        # 右端に余白を寄せて、見出しを左詰めにする
        header.columnconfigure(9, weight=1)

    # ------------------------------------------------------------------
    # 表示設定
    # ------------------------------------------------------------------
    def set_visible_pins(self, pins: set[str]) -> None:
        """描画するピンを指定する。"""
        self._visible = set(pins)

    def set_window_seconds(self, seconds: float | None) -> None:
        """直近何秒を表示するかを指定する。None なら全期間。"""
        self._window_seconds = seconds

    def value_range(self) -> tuple[float, float]:
        """いま描いている縦軸の範囲。自動のときは表示中の値から決まる。"""
        if self._manual_range is not None:
            return self._manual_range
        return self._store.value_range(self._drawn_pins())

    # ------------------------------------------------------------------
    # 縦軸の操作
    # ------------------------------------------------------------------
    def _on_auto_toggled(self) -> None:
        if self._auto_var.get():
            self._manual_range = None
            for widget in (self._min_entry, self._max_entry, self._min_scale, self._max_scale):
                widget.configure(state="disabled")
        else:
            # いまの範囲を初期値にしておくと、そこから数値でもバーでも詰めていける
            low, high = self.value_range()
            self._manual_range = (low, high)
            self._reset_slider_domain(low, high)
            self._show_range(low, high)
            for widget in (self._min_entry, self._max_entry, self._min_scale, self._max_scale):
                widget.configure(state="normal")
        self.redraw()

    def _on_range_entered(self, _event: tk.Event) -> None:
        if self._auto_var.get() or self._validating:
            return

        texts = (self._min_var.get(), self._max_var.get())
        try:
            low, high = (float(text) for text in texts)
        except ValueError:
            self._reject_range("最小・最大には数値を入力してください。")
            return
        if low >= high:
            self._reject_range("最小より大きい値を最大に入力してください。")
            return

        # バーの範囲外を数値で入れたときは、バー側の目盛りを広げて合わせる
        domain_low, domain_high = self._slider_domain
        if low < domain_low or high > domain_high:
            self._reset_slider_domain(low, high)
        self._manual_range = (low, high)
        self._show_range(low, high)
        self.redraw()

    def _on_min_slider(self, value: str) -> None:
        """最小のバーを動かしたとき。最大を下回らないところで止める。"""
        if self._syncing or self._auto_var.get():
            return
        _, high = self._manual_range or self.value_range()
        low = min(float(value), high - self._slider_step())
        self._manual_range = (low, high)
        self._show_range(low, high)
        self.redraw()

    def _on_max_slider(self, value: str) -> None:
        """最大のバーを動かしたとき。最小を上回らないところで止める。"""
        if self._syncing or self._auto_var.get():
            return
        low, _ = self._manual_range or self.value_range()
        high = max(float(value), low + self._slider_step())
        self._manual_range = (low, high)
        self._show_range(low, high)
        self.redraw()

    def _slider_step(self) -> float:
        """バーで動かせる最小の刻み。最小と最大が重ならないようにするために使う。"""
        domain_low, domain_high = self._slider_domain
        return (domain_high - domain_low) / 200.0

    def _reset_slider_domain(self, low: float, high: float) -> None:
        """バーで動かせる範囲を、いまの値の上下に同じだけ広げて決める。"""
        span = (high - low) or 1.0
        self._slider_domain = (low - span, high + span)
        # 目盛りを変えると値が丸められてバーの command が飛ぶため、その間は反応を止める
        self._syncing = True
        try:
            for scale in (self._min_scale, self._max_scale):
                scale.configure(from_=self._slider_domain[0], to=self._slider_domain[1])
        finally:
            self._syncing = False

    def _show_range(self, low: float, high: float) -> None:
        """数値欄とバーを、いまの範囲に合わせる。"""
        self._syncing = True
        try:
            self._min_var.set(_format_number(low))
            self._max_var.set(_format_number(high))
            self._min_scale.set(low)
            self._max_scale.set(high)
        finally:
            self._syncing = False

    def _reject_range(self, message: str) -> None:
        """入力を差し戻し、直前の範囲に戻す。"""
        self._validating = True
        try:
            messagebox.showerror("縦軸の指定", message, parent=self)
            low, high = self.value_range()
            self._show_range(low, high)
        finally:
            self._validating = False

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

        pins = self._drawn_pins()
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
        y_min, y_max = self.value_range()
        x_span = x_max - x_min or 1.0
        y_span = y_max - y_min or 1.0

        def to_x(seconds: float) -> float:
            return left + (seconds - x_min) / x_span * plot_width

        def to_y(value: float) -> float:
            y = top + plot_height - (value - y_min) / y_span * plot_height
            # 範囲を指定したときは、はみ出した線が目盛りに被らないよう枠内に収める
            return min(max(y, top), top + plot_height)

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

    def _drawn_pins(self) -> list[str]:
        """このグラフに描くピンを、系列の並び順で返す。"""
        return [pin for pin in self._store.pins() if pin in self._visible]

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
                text=self._format_time(x_min + ratio * x_span),
                anchor="n",
                fill=_TEXT,
                font=_FONT,
            )

    def _format_time(self, elapsed: float) -> str:
        """横軸の目盛りを PC の時刻（時:分:秒）にする。

        受信した時刻は time.time()、つまり PC の時計をそのまま使っている。
        まだ 1 件も受信していない間は経過秒で出す。
        """
        start = self._store.start_timestamp
        if start is None:
            return f"{elapsed:.1f}s"
        return dt.datetime.fromtimestamp(start + elapsed).strftime("%H:%M:%S")

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
