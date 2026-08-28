"""スケジュール登録の小窓。

Windows タスクスケジューラに登録する。常駐プロセスは持たない。
実行日の前日に確認実行を仕込めるようにしてあるのは、月 1 回しか動かさない
アプリで「当日に初めて壊れているのを知る」のを避けるため。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from logic import schedule

_TICK_MS = 100


class ScheduleDialog(tk.Toplevel):
    """月次スケジュールの登録・解除。"""

    def __init__(self, master: tk.Misc, scenario: str) -> None:
        super().__init__(master)
        self._scenario = scenario
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._tick_id: str | None = None

        self.title(f"スケジュール - {scenario}")
        self.minsize(470, 620)
        self.resizable(False, True)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._day_var = tk.StringVar(value="1")
        self._time_var = tk.StringVar(value="09:00")
        self._dry_var = tk.BooleanVar(value=True)
        self._dry_day_var = tk.StringVar(value="1")
        self._dry_time_var = tk.StringVar(value="08:00")
        # 空にしておくと「何も表示されない」状態になるので、最初から文言を入れる
        self._status_var = tk.StringVar(value="登録状況を確認しています...")

        self._build_widgets()
        self._tick()
        self._refresh_status()

    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        frame = ttk.Frame(self, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(3, weight=1)

        ttk.Label(
            frame,
            text=f"「{self._scenario}」を毎月決まった日時に自動で実行します。\n"
            "日時を決めて［登録する］を押すと、Windows のタスクスケジューラに"
            "登録されます。このアプリを開いていなくても動きます。",
            wraplength=400,
            justify="left",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))

        ttk.Label(frame, text="実行").grid(row=1, column=0, sticky="w")
        ttk.Label(frame, text="毎月").grid(row=1, column=1, sticky="e", padx=4)
        ttk.Spinbox(
            frame, from_=1, to=31, textvariable=self._day_var, width=5
        ).grid(row=1, column=2, sticky="w")
        ttk.Label(frame, text="日").grid(row=1, column=3, sticky="w")

        ttk.Label(frame, text="時刻").grid(row=2, column=1, sticky="e", padx=4)
        ttk.Entry(frame, textvariable=self._time_var, width=7).grid(
            row=2, column=2, sticky="w"
        )
        ttk.Label(frame, text="（09:00 の形式）").grid(row=2, column=3, sticky="w")

        ttk.Separator(frame, orient="horizontal").grid(
            row=3, column=0, columnspan=4, sticky="ew", pady=10
        )

        ttk.Checkbutton(
            frame,
            text="前もって確認実行する（壊れていないかを事前に知るため）",
            variable=self._dry_var,
        ).grid(row=4, column=0, columnspan=4, sticky="w")

        ttk.Label(frame, text="確認").grid(row=5, column=0, sticky="w")
        ttk.Label(frame, text="毎月").grid(row=5, column=1, sticky="e", padx=4)
        ttk.Spinbox(
            frame, from_=1, to=31, textvariable=self._dry_day_var, width=5
        ).grid(row=5, column=2, sticky="w")
        ttk.Label(frame, text="日").grid(row=5, column=3, sticky="w")

        ttk.Label(frame, text="時刻").grid(row=6, column=1, sticky="e", padx=4)
        ttk.Entry(frame, textvariable=self._dry_time_var, width=7).grid(
            row=6, column=2, sticky="w"
        )

        ttk.Label(
            frame,
            text="※ UI を操作するため、実行時刻に PC がログオン状態である必要があります。"
            "画面がロックされていると動きません。",
            foreground="#b7791f",
            wraplength=420,
        ).grid(row=7, column=0, columnspan=4, sticky="w", pady=(10, 0))

        ttk.Label(
            frame, textvariable=self._status_var, foreground="#555555", wraplength=420
        ).grid(row=8, column=0, columnspan=4, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=9, column=0, columnspan=4, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="登録する", command=self._on_register).grid(
            row=0, column=0, padx=4
        )
        ttk.Button(
            buttons, text="このシナリオの登録を解除", command=self._on_unregister
        ).grid(row=0, column=1, padx=4)

        # --- 全シナリオの登録一覧 ------------------------------------
        ttk.Separator(frame, orient="horizontal").grid(
            row=10, column=0, columnspan=4, sticky="ew", pady=12
        )
        ttk.Label(
            frame,
            text="このアプリが登録しているスケジュール（全シナリオ）",
        ).grid(row=11, column=0, columnspan=4, sticky="w")

        table = ttk.Frame(frame)
        table.grid(row=12, column=0, columnspan=4, sticky="nsew", pady=(4, 0))
        table.columnconfigure(0, weight=1)
        frame.rowconfigure(12, weight=1)

        self._tree = ttk.Treeview(
            table,
            columns=("scenario", "kind", "when", "next"),
            show="headings",
            height=6,
            selectmode="browse",
        )
        for key, text, width in (
            ("scenario", "シナリオ", 130),
            ("kind", "種類", 70),
            ("when", "実行日時", 130),
            ("next", "次回", 130),
        ):
            self._tree.heading(key, text=text)
            self._tree.column(key, width=width, anchor="w")
        self._tree.grid(row=0, column=0, sticky="nsew")

        bar = ttk.Scrollbar(table, orient="vertical", command=self._tree.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self._tree.configure(yscrollcommand=bar.set)

        bottom = ttk.Frame(frame)
        bottom.grid(row=13, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(0, weight=1)

        ttk.Button(
            bottom, text="選んだものを削除", command=self._on_delete_selected
        ).grid(row=0, column=1, padx=4)
        ttk.Button(bottom, text="更新", command=self._refresh_status, width=8).grid(
            row=0, column=2, padx=4
        )
        ttk.Button(bottom, text="閉じる", command=self._close, width=8).grid(
            row=0, column=3
        )

    # ------------------------------------------------------------------
    def _run_async(self, task, done_message: str) -> None:
        """schtasks の呼び出しは待たされることがあるので別スレッドで行う。"""

        def worker() -> None:
            try:
                task()
            except schedule.ScheduleError as exc:
                self._queue.put(("error", exc))
            else:
                self._queue.put(("done", done_message))

        threading.Thread(target=worker, name="schtasks", daemon=True).start()

    # メソッド名を _register / _unregister にしないこと。
    # tkinter の Misc._register(func) を上書きしてしまい、Toplevel の
    # 初期化中（protocol の登録）に TypeError で落ちる。
    def _on_register(self) -> None:
        try:
            day = int(self._day_var.get())
            dry_day = int(self._dry_day_var.get())
        except ValueError:
            messagebox.showerror("エラー", "日にちは数字で入力してください。",
                                 parent=self)
            return

        time_of_day = self._time_var.get().strip()
        dry_time = self._dry_time_var.get().strip()
        with_dry = self._dry_var.get()

        def task() -> None:
            schedule.register_monthly(
                self._scenario, day=day, time_of_day=time_of_day
            )
            if with_dry:
                schedule.register_monthly(
                    self._scenario,
                    day=dry_day,
                    time_of_day=dry_time,
                    dry_run=True,
                )
            else:
                schedule.unregister(self._scenario, dry_run=True)

        self._run_async(task, "登録しました。")

    def _on_unregister(self) -> None:
        def task() -> None:
            schedule.unregister(self._scenario)
            schedule.unregister(self._scenario, dry_run=True)

        self._run_async(task, "解除しました。")

    def _on_delete_selected(self) -> None:
        """一覧で選んだタスクを消す。別シナリオのものも消せる。

        シナリオを消したあとに取り残されたタスクを片付けられるようにするため。
        """
        selection = self._tree.selection()
        if not selection:
            messagebox.showinfo(
                "削除", "一覧から削除するものを選んでください。", parent=self
            )
            return

        name = selection[0]
        values = self._tree.item(name, "values")
        if not messagebox.askyesno(
            "確認",
            f"「{values[0]}」の{values[1]}（{values[2]}）を削除しますか。",
            parent=self,
        ):
            return

        self._run_async(lambda: schedule.unregister_task(name), "削除しました。")

    def _refresh_status(self) -> None:
        def worker() -> None:
            try:
                tasks = schedule.list_tasks()
            except schedule.ScheduleError as exc:
                self._queue.put(("error", exc))
            else:
                self._queue.put(("status", tasks))

        threading.Thread(target=worker, name="schtasks-list", daemon=True).start()

    def _load_current(self, tasks: list) -> None:
        """登録済みの日時を入力欄に反映する。

        開くたびに既定値（1 日 09:00）に戻ると、今どう登録されているのかが
        分からないまま上書きしてしまう。
        """
        for task in tasks:
            if not task.time_of_day:
                continue
            if task.dry_run:
                self._dry_var.set(True)
                self._dry_day_var.set(str(task.day or 1))
                self._dry_time_var.set(task.time_of_day)
            else:
                self._day_var.set(str(task.day or 1))
                self._time_var.set(task.time_of_day)

        if not any(task.dry_run for task in tasks):
            self._dry_var.set(False)

    def _fill_table(self, tasks: list) -> None:
        self._tree.delete(*self._tree.get_children())
        for task in tasks:
            self._tree.insert(
                "",
                "end",
                iid=task.name,
                values=(task.scenario, task.kind, task.schedule, task.next_run),
            )

    # ------------------------------------------------------------------
    def _tick(self) -> None:
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break

            if kind == "error":
                self._status_var.set("失敗しました。")
                messagebox.showerror("エラー", str(payload), parent=self)
            elif kind == "done":
                self._status_var.set(str(payload))
                self._refresh_status()
            elif kind == "status":
                self._fill_table(payload)

                mine = [t for t in payload if t.scenario == self._scenario]
                if mine:
                    lines = [
                        f"✓ {task.kind}　{task.schedule}　次回 {task.next_run}"
                        for task in mine
                    ]
                    self._status_var.set(
                        f"「{self._scenario}」は登録済み\n" + "\n".join(lines)
                    )
                    self._load_current(mine)
                else:
                    self._status_var.set(
                        f"「{self._scenario}」はまだ登録されていません。"
                        "日時を決めて［登録する］を押してください。"
                    )

        self._tick_id = self.after(_TICK_MS, self._tick)

    def _close(self) -> None:
        if self._tick_id is not None:
            self.after_cancel(self._tick_id)
            self._tick_id = None
        self.destroy()
