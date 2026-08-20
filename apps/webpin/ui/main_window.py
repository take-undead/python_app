"""メイン画面。マイコンからのピン情報の受信・CSV ロギング・グラフ表示を行う。"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from logic.paths import data_dir
from logic.protocol import CHART_COUNT, CHART_HIDDEN, default_chart_assignment
from logic.recorder import CsvRecorder, RecorderError, default_log_path
from logic.series import DEFAULT_CAPACITY, SeriesStore
from logic.ws_client import (
    DEFAULT_OCTETS,
    LOCAL_PREFIX,
    OCTET_MAX,
    OCTET_MIN,
    PinClient,
    WsError,
    url_from_octets,
)
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

# CSV の既定の保存先（カレントディレクトリに依存させない）。
# 実行ファイルのときは exe と同じフォルダの logs/ になる
LOG_DIR = data_dir() / "logs"

# ピン一覧で選ぶグラフ番号の表示。並び順がそのままグラフ番号（0 は非表示）
_CHART_LABELS: tuple[str, ...] = ("―", *(str(index) for index in range(1, CHART_COUNT + 1)))

# 出力（DOUT）のボタンを並べる列数
_DOUT_COLUMNS = 2


def _dout_label(name: str, state: bool) -> str:
    """出力ボタンの文字。いまの状態が一目で分かるようにする。"""
    return f"{name}: {'ON' if state else 'OFF'}"


class MainWindow(ttk.Frame):
    """webpin のメイン画面。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=8)

        self._client: PinClient | None = None
        self._recorder: CsvRecorder | None = None
        self._store = SeriesStore(capacity=DEFAULT_CAPACITY)
        self._poll_id: str | None = None
        self._redraw_id: str | None = None

        # ピンごとの「どのグラフに出すか」と最新値ラベル。ピンは受信して初めて分かる
        self._pin_chart_vars: dict[str, tk.StringVar] = {}
        self._pin_value_vars: dict[str, tk.StringVar] = {}

        # ON/OFF を指示できる DOUT。系列名 -> GPIO 番号 / 状態 / ボタンの文字 / ボタン
        self._dout_pins: dict[str, int] = {}
        self._dout_vars: dict[str, tk.BooleanVar] = {}
        self._dout_label_vars: dict[str, tk.StringVar] = {}
        self._dout_buttons: dict[str, ttk.Checkbutton] = {}

        # 接続先は 192.168.<第 3>.<第 4> の 2 つだけ入力させる
        self._octet3_var = tk.StringVar(value=str(DEFAULT_OCTETS[0]))
        self._octet4_var = tk.StringVar(value=str(DEFAULT_OCTETS[1]))
        self._url_var = tk.StringVar(value="")  # 接続時に組み立てた URL を保持する
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

        # グラフを縦に並べる。桁の違う系列を別の軸で見られるようにするため
        self._charts: list[ChartFrame] = []
        for index in range(CHART_COUNT):
            chart = ChartFrame(body, self._store, title=f"グラフ {index + 1}")
            chart.grid(row=index, column=0, sticky="nsew", pady=(0 if index == 0 else 6, 0))
            chart.set_window_seconds(_WINDOW_CHOICES[1][1])
            body.rowconfigure(index, weight=1)
            self._charts.append(chart)

        # 右側は上が出力（操作）、下がピン一覧（ロギングと表示）。役割が違うので分ける
        side = ttk.Frame(body)
        side.grid(row=0, column=1, rowspan=CHART_COUNT, sticky="ns", padx=(8, 0))
        side.rowconfigure(1, weight=1)

        self._build_dout_panel(side)
        self._build_pin_panel(side)
        self._apply_assignments()

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

        # ローカルネットワーク前提。192.168. は固定で、残り 2 つだけ入力させる
        address = ttk.Frame(control)
        address.grid(row=0, column=1, padx=(0, 8))
        ttk.Label(address, text=LOCAL_PREFIX).grid(row=0, column=0)
        self._octet3_entry = ttk.Spinbox(
            address,
            textvariable=self._octet3_var,
            from_=OCTET_MIN,
            to=OCTET_MAX,
            width=4,
            justify="right",
        )
        self._octet3_entry.grid(row=0, column=1)
        ttk.Label(address, text=".").grid(row=0, column=2, padx=1)
        self._octet4_entry = ttk.Spinbox(
            address,
            textvariable=self._octet4_var,
            from_=OCTET_MIN,
            to=OCTET_MAX,
            width=4,
            justify="right",
        )
        self._octet4_entry.grid(row=0, column=3)

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

    def _build_dout_panel(self, master: tk.Misc) -> None:
        """DOUT を ON/OFF するボタンを並べる枠。

        こちらから操作する機能で、記録・表示（ピン一覧）とは役割が違うため分けている。
        ボタンはマイコンから DOUT が届いた時点で作る。
        """
        self._dout_panel = ttk.LabelFrame(master, text="出力（DOUT）", padding=6)
        self._dout_panel.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for column in range(_DOUT_COLUMNS):
            self._dout_panel.columnconfigure(column, weight=1)

        self._dout_placeholder = ttk.Label(
            self._dout_panel, text="未受信", foreground="#808080"
        )
        self._dout_placeholder.grid(row=0, column=0, columnspan=_DOUT_COLUMNS, sticky="w")

    def _add_dout_buttons(self, names: list[str]) -> None:
        """新しく現れた DOUT の ON/OFF ボタンを追加する。"""
        self._dout_placeholder.grid_remove()

        for name in names:
            position = len(self._dout_buttons)
            row, column = divmod(position, _DOUT_COLUMNS)

            state_var = tk.BooleanVar(value=bool(self._store.latest(name)))
            label_var = tk.StringVar(value=_dout_label(name, state_var.get()))
            self._dout_vars[name] = state_var
            self._dout_label_vars[name] = label_var

            # Toolbutton にすると、押し込み表示のボタンとして扱える
            button = ttk.Checkbutton(
                self._dout_panel,
                style="Toolbutton",
                textvariable=label_var,
                variable=state_var,
                width=12,
                command=lambda pin=name: self._on_dout_toggled(pin),
                state="normal" if self._client is not None else "disabled",
            )
            button.grid(row=row, column=column, padx=2, pady=2, sticky="ew")
            self._dout_buttons[name] = button

    def _build_pin_panel(self, master: tk.Misc) -> None:
        """右側のピン一覧。系列ごとに、出すグラフの番号と最新値を並べる。"""
        panel = ttk.LabelFrame(master, text="ピン（出力先のグラフ）", padding=6)
        panel.grid(row=1, column=0, sticky="ns")
        panel.rowconfigure(0, weight=1)

        # 系列が増えても収まるようにスクロールできるようにする
        canvas = tk.Canvas(panel, width=340, highlightthickness=0, borderwidth=0)
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
        self._pin_placeholder.grid(row=0, column=0, columnspan=5, sticky="w")

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def _on_connect(self) -> None:
        if self._client is not None:
            return

        try:
            url = url_from_octets(self._octet3_var.get(), self._octet4_var.get())
        except WsError as exc:
            messagebox.showerror("接続エラー", str(exc), parent=self)
            return

        self._url_var.set(url)
        client = PinClient(url)
        client.start()
        self._client = client

        self._connect_button.configure(state="disabled")
        self._octet3_entry.configure(state="disabled")
        self._octet4_entry.configure(state="disabled")
        self._disconnect_button.configure(state="normal")
        self._update_dout_buttons()
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
        self._redraw_charts()

    def _on_window_changed(self, _event: tk.Event) -> None:
        label = self._window_var.get()
        for name, seconds in _WINDOW_CHOICES:
            if name == label:
                for chart in self._charts:
                    chart.set_window_seconds(seconds)
                break
        self._redraw_charts()

    def _on_chart_changed(self, _event: tk.Event) -> None:
        self._apply_assignments()

    # ------------------------------------------------------------------
    # DOUT の ON/OFF
    # ------------------------------------------------------------------
    def _on_dout_toggled(self, name: str) -> None:
        """ピン一覧の「出力」を押したとき、マイコンに ON/OFF を指示する。"""
        var = self._dout_vars[name]
        client = self._client
        gpio = self._dout_pins.get(name)

        if client is None or gpio is None:
            messagebox.showerror(
                "送信エラー", "接続していないため指示できません。", parent=self
            )
            self._sync_dout_states()
            return

        state = var.get()
        try:
            client.set_dout(gpio, state)
        except WsError as exc:
            messagebox.showerror("送信エラー", str(exc), parent=self)
            self._sync_dout_states()
            return

        # 実際の状態はマイコンからの次の state で確定する
        self._dout_label_vars[name].set(_dout_label(name, state))
        self._status_var.set(f"{name}（GP{gpio}）を {'ON' if state else 'OFF'} にしました。")

    def _sync_dout_states(self) -> None:
        """ボタンの見た目を、マイコンが返している状態に合わせる。"""
        for name, var in self._dout_vars.items():
            value = self._store.latest(name)
            if value is not None:
                var.set(value >= 0.5)
            self._dout_label_vars[name].set(_dout_label(name, var.get()))

    def _update_dout_buttons(self) -> None:
        """接続していないときは ON/OFF を押せないようにする。"""
        state = "normal" if self._client is not None else "disabled"
        for button in self._dout_buttons.values():
            button.configure(state=state)

    def _apply_assignments(self) -> None:
        """ピン一覧で選ばれた番号どおりに、各グラフの系列を入れ替える。"""
        assignment = {
            pin: _CHART_LABELS.index(var.get())
            for pin, var in self._pin_chart_vars.items()
        }
        used = set(assignment.values()) - {CHART_HIDDEN}

        for number, chart in enumerate(self._charts, start=1):
            chart.set_visible_pins(
                {pin for pin, target in assignment.items() if target == number}
            )
            # 何も割り当てられていないグラフは畳んで、残りに高さを譲る。
            # ただしどこにも割り当てが無いときは、1 つ目だけ枠を残す
            if number in used or (number == 1 and not used):
                chart.grid()
            else:
                chart.grid_remove()

        self._redraw_charts()

    def _redraw_charts(self) -> None:
        for chart in self._charts:
            chart.redraw()

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
            known_pins = set(self._pin_chart_vars)
            for sample in samples:
                self._store.add(sample)
                # ON/OFF の指示には GPIO 番号が要る。ボタンを作る前に控える
                self._dout_pins.update(sample.outputs)

            new_pins = [
                pin for pin in self._store.pins() if pin not in known_pins
            ]
            if new_pins:
                self._add_pin_rows(new_pins)

            new_douts = [
                name for name in self._dout_pins if name not in self._dout_buttons
            ]
            if new_douts:
                self._add_dout_buttons(new_douts)

            # 実際の出力状態はマイコンが返す値が正。ボタンはそれに追従させる
            self._sync_dout_states()

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
        defaults = default_chart_assignment(all_pins)

        for pin in pins:
            row = len(self._pin_chart_vars)
            index = all_pins.index(pin)

            chart_var = tk.StringVar(
                value=_CHART_LABELS[defaults.get(pin, CHART_HIDDEN)]
            )
            value_var = tk.StringVar(value="-")
            self._pin_chart_vars[pin] = chart_var
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

            ttk.Label(self._pin_container, text=pin).grid(row=row, column=1, sticky="w")

            chart_combo = ttk.Combobox(
                self._pin_container,
                textvariable=chart_var,
                values=list(_CHART_LABELS),
                state="readonly",
                width=3,
            )
            chart_combo.grid(row=row, column=2, padx=(4, 0))
            chart_combo.bind("<<ComboboxSelected>>", self._on_chart_changed)

            ttk.Label(
                self._pin_container, textvariable=value_var, anchor="e", width=8
            ).grid(row=row, column=3, sticky="e", padx=(4, 0))

        self._apply_assignments()

    # ------------------------------------------------------------------
    # グラフの更新
    # ------------------------------------------------------------------
    def _schedule_redraw(self) -> None:
        self._redraw_id = self.after(_REDRAW_MS, self._redraw)

    def _redraw(self) -> None:
        self._redraw_id = None
        self._redraw_charts()
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
        self._octet3_entry.configure(state="normal")
        self._octet4_entry.configure(state="normal")
        self._disconnect_button.configure(state="disabled")
        self._update_dout_buttons()

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
