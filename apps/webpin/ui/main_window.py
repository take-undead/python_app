"""メイン画面。マイコンからのピン情報の受信・CSV ロギング・グラフ表示を行う。"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from logic.protocol import default_visible_pins
from logic.recorder import CsvRecorder, RecorderError, default_log_path
from logic.series import DEFAULT_CAPACITY, SeriesStore
from logic.ws_client import DEFAULT_URL, PinClient, WsError, normalize_url
from ui.chart import ChartFrame, color_for

# 受信キューを取り出す間隔（ミリ秒）。マイコン側は約 200ms 間隔で送ってくる
_POLL_MS = 100

# グラフを描き直す間隔（ミリ秒）
_REDRAW_MS = 200

# グラフの表示範囲の選択肢（表示名, 秒数）。None は全期間
_WINDOW_CHOICES: tuple[tuple[str, float | None], ...] = (
    ("10 秒", 10.0),
    ("30 秒", 30.0),
    ("1 分", 60.0),
    ("5 分", 300.0),
    ("全期間", None),
)

# CSV の既定の保存先（カレントディレクトリに依存させない）
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


class MainWindow(ttk.Frame):
    """webpin のメイン画面。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=8)

        self._client: PinClient | None = None
        self._recorder: CsvRecorder | None = None
        self._store = SeriesStore(capacity=DEFAULT_CAPACITY)
        self._poll_id: str | None = None
        self._redraw_id: str | None = None

        # ピンごとの表示チェックと最新値ラベル。ピンは受信して初めて分かる
        self._pin_vars: dict[str, tk.BooleanVar] = {}
        self._pin_value_vars: dict[str, tk.StringVar] = {}

        self._url_var = tk.StringVar(value=DEFAULT_URL)
        self._window_var = tk.StringVar(value=_WINDOW_CHOICES[1][0])
        self._status_var = tk.StringVar(value="接続先を確認して「接続」を押してください。")
        self._count_var = tk.StringVar(value="受信 0 件")
        self._log_var = tk.StringVar(value="記録していません。")

        self._build_widgets()

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._build_control_bar()

        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew", pady=8)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        self._chart = ChartFrame(body, self._store)
        self._chart.grid(row=0, column=0, sticky="nsew")
        self._chart.set_window_seconds(_WINDOW_CHOICES[1][1])

        self._build_pin_panel(body)

        status = ttk.Frame(self)
        status.grid(row=2, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        ttk.Label(status, textvariable=self._status_var, anchor="w").grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Label(status, textvariable=self._count_var, anchor="e").grid(
            row=0, column=1, padx=(8, 0)
        )

    def _build_control_bar(self) -> None:
        control = ttk.Frame(self)
        control.grid(row=0, column=0, sticky="ew")

        ttk.Label(control, text="接続先:").grid(row=0, column=0, padx=(0, 4))

        self._url_entry = ttk.Combobox(
            control,
            textvariable=self._url_var,
            values=[DEFAULT_URL, "ws://t-iot_mobile.local/ws"],
            width=32,
        )
        self._url_entry.grid(row=0, column=1, padx=(0, 8))

        self._connect_button = ttk.Button(
            control, text="接続", command=self._on_connect, width=8
        )
        self._connect_button.grid(row=0, column=2, padx=(0, 4))

        self._disconnect_button = ttk.Button(
            control, text="切断", command=self._on_disconnect, width=8, state="disabled"
        )
        self._disconnect_button.grid(row=0, column=3, padx=(0, 12))

        self._record_button = ttk.Button(
            control, text="記録開始", command=self._on_toggle_record, width=10
        )
        self._record_button.grid(row=0, column=4, padx=(0, 4))

        ttk.Button(control, text="クリア", command=self._on_clear, width=8).grid(
            row=0, column=5, padx=(0, 12)
        )

        ttk.Label(control, text="表示範囲:").grid(row=0, column=6, padx=(0, 4))
        window_combo = ttk.Combobox(
            control,
            textvariable=self._window_var,
            values=[label for label, _ in _WINDOW_CHOICES],
            state="readonly",
            width=8,
        )
        window_combo.grid(row=0, column=7)
        window_combo.bind("<<ComboboxSelected>>", self._on_window_changed)

        # 右端に余白を寄せて、操作部を左詰めにする
        control.columnconfigure(8, weight=1)

        ttk.Label(control, textvariable=self._log_var, anchor="e").grid(
            row=0, column=8, sticky="e"
        )

    def _build_pin_panel(self, master: tk.Misc) -> None:
        """右側のピン一覧。受信した系列を表示切り替えと最新値付きで並べる。"""
        panel = ttk.LabelFrame(master, text="ピン", padding=6)
        panel.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        panel.rowconfigure(0, weight=1)

        # 系列が増えても収まるようにスクロールできるようにする
        canvas = tk.Canvas(panel, width=250, highlightthickness=0, borderwidth=0)
        canvas.grid(row=0, column=0, sticky="ns")
        scrollbar = ttk.Scrollbar(panel, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)

        self._pin_container = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=self._pin_container, anchor="nw")
        self._pin_container.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window_id, width=event.width),
        )
        self._pin_container.columnconfigure(1, weight=1)

        self._pin_placeholder = ttk.Label(
            self._pin_container, text="未受信", foreground="#808080"
        )
        self._pin_placeholder.grid(row=0, column=0, columnspan=3, sticky="w")

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def _on_connect(self) -> None:
        if self._client is not None:
            return

        try:
            url = normalize_url(self._url_var.get())
        except WsError as exc:
            messagebox.showerror("接続エラー", str(exc), parent=self)
            return

        self._url_var.set(url)
        client = PinClient(url)
        client.start()
        self._client = client

        self._connect_button.configure(state="disabled")
        self._url_entry.configure(state="disabled")
        self._disconnect_button.configure(state="normal")
        self._status_var.set(f"{url} に接続しています...")

        self._schedule_poll()
        self._schedule_redraw()

    def _on_disconnect(self) -> None:
        self._stop_client()
        self._status_var.set("切断しました。")

    def _on_toggle_record(self) -> None:
        if self._recorder is not None:
            self._stop_record()
            return

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        default_path = default_log_path(LOG_DIR)
        selected = filedialog.asksaveasfilename(
            parent=self,
            title="ログの保存先",
            initialdir=str(LOG_DIR),
            initialfile=default_path.name,
            defaultextension=".csv",
            filetypes=[("CSV ファイル", "*.csv"), ("すべてのファイル", "*.*")],
        )
        if not selected:
            return

        recorder = CsvRecorder(Path(selected))
        try:
            recorder.open()
        except RecorderError as exc:
            messagebox.showerror("保存エラー", str(exc), parent=self)
            return

        self._recorder = recorder
        self._record_button.configure(text="記録停止")
        self._log_var.set(f"記録中: {recorder.path.name}（0 行）")

    def _stop_record(self) -> None:
        recorder = self._recorder
        self._recorder = None
        self._record_button.configure(text="記録開始")
        if recorder is None:
            return

        rows = recorder.row_count
        path = recorder.path
        try:
            recorder.close()
        except RecorderError as exc:
            messagebox.showerror("保存エラー", str(exc), parent=self)
            self._log_var.set("記録していません。")
            return

        self._log_var.set(f"保存しました: {path.name}（{rows} 行）")
        if recorder.unknown_pins:
            messagebox.showwarning(
                "記録",
                "記録の途中で新しいピンが現れたため、次のピンは保存されていません。\n"
                "記録を始め直すと列に含まれます。\n\n"
                + ", ".join(sorted(recorder.unknown_pins)),
                parent=self,
            )

    def _on_clear(self) -> None:
        self._store.clear()
        for var in self._pin_value_vars.values():
            var.set("-")
        self._count_var.set("受信 0 件")
        self._chart.redraw()

    def _on_window_changed(self, _event: tk.Event) -> None:
        label = self._window_var.get()
        for name, seconds in _WINDOW_CHOICES:
            if name == label:
                self._chart.set_window_seconds(seconds)
                break
        self._chart.redraw()

    def _on_pin_toggled(self) -> None:
        self._chart.set_visible_pins(
            {pin for pin, var in self._pin_vars.items() if var.get()}
        )
        self._chart.redraw()

    # ------------------------------------------------------------------
    # 受信の取り込み
    # ------------------------------------------------------------------
    def _schedule_poll(self) -> None:
        self._poll_id = self.after(_POLL_MS, self._poll)

    def _poll(self) -> None:
        self._poll_id = None

        client = self._client
        if client is None:
            return

        status = client.take_status()
        if status is not None:
            self._status_var.set(status)

        samples = client.poll()
        if samples:
            known_pins = set(self._pin_vars)
            for sample in samples:
                self._store.add(sample)

            new_pins = [
                pin for pin in self._store.pins() if pin not in known_pins
            ]
            if new_pins:
                self._add_pin_rows(new_pins)

            recorder = self._recorder
            if recorder is not None:
                try:
                    for sample in samples:
                        recorder.write(sample)
                    recorder.flush()
                except RecorderError as exc:
                    self._recorder = None
                    self._record_button.configure(text="記録開始")
                    self._log_var.set("記録を中断しました。")
                    messagebox.showerror("保存エラー", str(exc), parent=self)
                else:
                    self._log_var.set(
                        f"記録中: {recorder.path.name}（{recorder.row_count} 行）"
                    )

            for pin, var in self._pin_value_vars.items():
                value = self._store.latest(pin)
                var.set("-" if value is None else _format_value(value))

            self._count_var.set(f"受信 {self._store.sample_count} 件")

        self._schedule_poll()

    def _add_pin_rows(self, pins: list[str]) -> None:
        """新しく現れた系列を、右側のピン一覧に追加する。"""
        self._pin_placeholder.grid_remove()

        all_pins = self._store.pins()
        visible = default_visible_pins(all_pins)

        for pin in pins:
            row = len(self._pin_vars)
            index = all_pins.index(pin)

            checked = tk.BooleanVar(value=pin in visible)
            value_var = tk.StringVar(value="-")
            self._pin_vars[pin] = checked
            self._pin_value_vars[pin] = value_var

            # 線の色をそのまま凡例にする
            swatch = tk.Frame(
                self._pin_container,
                background=color_for(index),
                width=12,
                height=12,
            )
            swatch.grid(row=row, column=0, padx=(0, 4), pady=1)
            swatch.grid_propagate(False)

            ttk.Checkbutton(
                self._pin_container,
                text=pin,
                variable=checked,
                command=self._on_pin_toggled,
            ).grid(row=row, column=1, sticky="w")

            ttk.Label(
                self._pin_container, textvariable=value_var, anchor="e", width=8
            ).grid(row=row, column=2, sticky="e", padx=(4, 0))

        self._on_pin_toggled()

    # ------------------------------------------------------------------
    # グラフの更新
    # ------------------------------------------------------------------
    def _schedule_redraw(self) -> None:
        self._redraw_id = self.after(_REDRAW_MS, self._redraw)

    def _redraw(self) -> None:
        self._redraw_id = None
        self._chart.redraw()
        if self._client is not None:
            self._schedule_redraw()

    # ------------------------------------------------------------------
    # 後始末
    # ------------------------------------------------------------------
    def _stop_client(self) -> None:
        for attr in ("_poll_id", "_redraw_id"):
            after_id = getattr(self, attr)
            if after_id is not None:
                self.after_cancel(after_id)
                setattr(self, attr, None)

        client = self._client
        self._client = None
        if client is not None:
            client.stop()

        self._connect_button.configure(state="normal")
        self._url_entry.configure(state="normal")
        self._disconnect_button.configure(state="disabled")

    def shutdown(self) -> None:
        """ウィンドウを閉じるときに呼ぶ。接続とログを確実に閉じる。"""
        self._stop_client()
        if self._recorder is not None:
            self._stop_record()


def _format_value(value: float) -> str:
    """最新値ラベル用に、桁数を抑えて数値を文字列にする。"""
    if value == int(value):
        return str(int(value))
    return f"{value:.3f}"
