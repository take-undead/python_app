"""メイン画面。

左に手順一覧、右に選択中の手順の設定、下に実行ログ。
JSON は画面に出さない。操作の追加はピッカーとドロップダウンで行う。

時間のかかる処理（実行・確認実行）は別スレッドに逃がし、進捗を queue で
受け取って after() で画面に反映する。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Callable

from logic import schedule, storage
from logic.actions import ACTIONS, Scenario, ScenarioError, Step
from logic.picker import ElementRef
from logic.runner import RunReport, Runner
from ui.app_chooser import AppChooser
from ui.picker_dialog import PickerDialog
from ui.schedule_dialog import ScheduleDialog
from ui.step_form import StepForm

_TICK_MS = 100

_LOG_STYLE: dict[str, dict[str, str]] = {
    "ok": {"foreground": "#1a7f37"},
    "error": {"foreground": "#c0392b"},
    "warn": {"foreground": "#b7791f"},
    "step": {"foreground": "#1f2937"},
    "info": {"foreground": "#6b7280"},
}


class MainWindow(ttk.Frame):
    """Win RPA のメイン画面。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=8)

        self._queue: queue.Queue[tuple[Callable[[Any], None], Any]] = queue.Queue()
        self._busy = False
        self._tick_id: str | None = None
        self._runner: Runner | None = None

        self._scenario = Scenario(name="新しいシナリオ")
        self._selected = -1
        # 保存時点の内容。未保存の編集があるかの判定に使う
        self._saved_state: dict[str, Any] = self._scenario.to_dict()

        self._status_var = tk.StringVar(value="準備完了")
        self._scenario_var = tk.StringVar()

        self._build_widgets()
        self._tick()
        self._reload_scenario_list()

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=3)
        self.rowconfigure(2, weight=1)

        # 「手順を追加」は最初に押すボタンなので、他より目立たせる。
        # ttk.Menubutton は枠が出ずラベルに見えてしまうため使わない
        style = ttk.Style(self)
        style.configure(
            "Add.TButton", font=("Meiryo UI", 10, "bold"), padding=(12, 6)
        )

        self._build_toolbar()
        self._build_body()
        self._build_log()

        ttk.Label(self, textvariable=self._status_var, anchor="w").grid(
            row=3, column=0, sticky="ew", pady=(4, 0)
        )

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        bar.columnconfigure(2, weight=1)

        ttk.Label(bar, text="シナリオ").grid(row=0, column=0, padx=(0, 6))

        self._scenario_box = ttk.Combobox(
            bar, textvariable=self._scenario_var, state="readonly", width=28
        )
        self._scenario_box.grid(row=0, column=1)
        self._scenario_box.bind(
            "<<ComboboxSelected>>", lambda _e: self._on_scenario_selected()
        )

        buttons = ttk.Frame(bar)
        buttons.grid(row=0, column=3, sticky="e")

        # シナリオそのものを扱う操作
        for index, (text, command) in enumerate(
            (
                ("新規", self._on_new),
                ("名前を変更", self._on_rename),
                ("保存", self._on_save),
                ("削除", self._on_delete),
            )
        ):
            ttk.Button(buttons, text=text, command=command).grid(
                row=0, column=index, padx=2
            )

        # スケジュール登録はシナリオの編集ではなく「いつ動かすか」の設定なので、
        # 区切り線と余白を入れて上の 4 つと分ける
        ttk.Separator(buttons, orient="vertical").grid(
            row=0, column=4, sticky="ns", padx=12, pady=2
        )
        ttk.Button(
            buttons, text="スケジュール...", command=self._on_schedule
        ).grid(row=0, column=5, padx=2)

    def _build_body(self) -> None:
        body = ttk.Panedwindow(self, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew")

        left = ttk.Frame(body, padding=(0, 0, 4, 0))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        body.add(left, weight=2)

        # 手順の追加は最初に押すものなので、一覧の上に大きく置く。
        # 一覧の下に小さく置くと初見で見つけられない
        header = ttk.Frame(left)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text="手順", font=("Meiryo UI", 10, "bold")).grid(
            row=0, column=0, sticky="w"
        )

        self._add_menu = tk.Menu(self, tearoff=False)
        for action, spec in ACTIONS.items():
            self._add_menu.add_command(
                label=spec.label, command=lambda a=action: self._on_add_step(a)
            )

        self._add_button = ttk.Button(
            header,
            text="＋ 手順を追加  ▾",
            style="Add.TButton",
            command=self._popup_add_menu,
        )
        self._add_button.grid(row=0, column=1, sticky="e")

        self._steps = tk.Listbox(left, font=("Meiryo UI", 10), activestyle="none")
        self._steps.grid(row=1, column=0, sticky="nsew")
        self._steps.bind("<<ListboxSelect>>", lambda _e: self._on_step_selected())

        scrollbar = ttk.Scrollbar(left, orient="vertical", command=self._steps.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self._steps.configure(yscrollcommand=scrollbar.set)

        # 手順が 0 件のときに、次に何をすればよいかを一覧の中に出す
        self._empty_hint = ttk.Label(
            left,
            text="まだ手順がありません。\n\n"
            "右上の［＋ 手順を追加］から\n操作を選んでください。",
            justify="center",
            anchor="center",
            foreground="#888888",
        )

        controls = ttk.Frame(left)
        controls.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        for index, (text, command, width) in enumerate(
            (
                ("↑", self._on_move_up, 3),
                ("↓", self._on_move_down, 3),
                ("複製", self._on_duplicate, 6),
                ("削除", self._on_delete_step, 6),
            )
        ):
            ttk.Button(controls, text=text, command=command, width=width).grid(
                row=0, column=index, padx=1
            )
        controls.columnconfigure(4, weight=1)

        right = ttk.Frame(body)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        body.add(right, weight=3)

        self._form = StepForm(right, self, self._refresh_steps)
        self._form.grid(row=0, column=0, sticky="nsew")

        run_bar = ttk.Frame(right, padding=(12, 0))
        run_bar.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        run_bar.columnconfigure(3, weight=1)

        self._upto_button = ttk.Button(
            run_bar, text="ここまで試し実行", command=self._on_run_upto
        )
        self._upto_button.grid(row=0, column=0, padx=(0, 4))

        self._dry_button = ttk.Button(
            run_bar, text="最初から確認実行", command=self._on_run_dry
        )
        self._dry_button.grid(row=0, column=1, padx=4)

        self._run_button = ttk.Button(
            run_bar, text="実行", command=self._on_run, width=10
        )
        self._run_button.grid(row=0, column=2, padx=4)

        self._cancel_button = ttk.Button(
            run_bar, text="中止", command=self._on_cancel, state="disabled", width=8
        )
        self._cancel_button.grid(row=0, column=4, sticky="e")

    def _build_log(self) -> None:
        frame = ttk.Frame(self)
        frame.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        ttk.Label(frame, text="実行ログ").grid(row=0, column=0, sticky="w")

        self._log = tk.Text(
            frame, height=8, font=("Meiryo UI", 9), state="disabled", wrap="word"
        )
        self._log.grid(row=1, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self._log.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self._log.configure(yscrollcommand=scrollbar.set)

        for kind, options in _LOG_STYLE.items():
            self._log.tag_configure(kind, **options)

    # ------------------------------------------------------------------
    # EditorContext（field_editors から呼ばれる）
    # ------------------------------------------------------------------
    def pick_element(self, on_picked: Callable[[ElementRef], None]) -> None:
        """要素ピッカーを開く。対象アプリを隠さないよう本体を小さくする。"""
        root = self.winfo_toplevel()
        root.iconify()

        def done(ref: ElementRef) -> None:
            root.deiconify()
            on_picked(ref)
            self._refresh_steps()

        dialog = PickerDialog(self, done)
        dialog.bind("<Destroy>", lambda _e: root.deiconify(), add="+")

    def choose_app(self, on_chosen: Callable[[dict[str, Any]], None]) -> None:
        AppChooser(self, lambda params: (on_chosen(params), self._refresh_steps()))

    def work_dir(self) -> Path:
        return self._scenario.resolved_work_dir(storage.work_root())

    # ------------------------------------------------------------------
    # シナリオ
    # ------------------------------------------------------------------
    def _reload_scenario_list(self, select: str | None = None) -> None:
        names = storage.list_names()
        self._scenario_box.configure(values=names)
        if select:
            self._scenario_var.set(select)
        elif names and not self._scenario_var.get():
            self._scenario_var.set(names[0])
            self._on_scenario_selected()

    def _is_dirty(self) -> bool:
        """未保存の編集があるか。"""
        self._form.collect()
        return self._scenario.to_dict() != self._saved_state

    def _confirm_discard(self) -> bool:
        """未保存の編集を捨ててよいか確かめる。

        シナリオを切り替えると編集中の内容が消える。月 1 回しか触らない
        アプリなので、黙って消えると次に気づくのが 1 か月後になる。
        """
        if not self._is_dirty():
            return True
        return messagebox.askyesno(
            "未保存の変更があります",
            f"「{self._scenario.name}」の変更は保存されていません。\n"
            "破棄して続けますか。",
            parent=self,
        )

    def _on_scenario_selected(self) -> None:
        name = self._scenario_var.get()
        if not name or name == self._scenario.name:
            return

        if not self._confirm_discard():
            self._scenario_var.set(self._scenario.name)
            return

        try:
            self._scenario = storage.load(name)
        except ScenarioError as exc:
            self._scenario_var.set(self._scenario.name)
            messagebox.showerror("エラー", str(exc), parent=self)
            return

        self._selected = -1
        self._saved_state = self._scenario.to_dict()
        self._refresh_steps()
        self._form.show(None)
        self._status_var.set(f"「{name}」を開きました（{len(self._scenario.steps)} 手順）")

    def _on_new(self) -> None:
        if not self._confirm_discard():
            return

        name = simpledialog.askstring(
            "新しいシナリオ", "シナリオ名を入力してください。", parent=self
        )
        if not name:
            return

        if storage.exists(name) and not messagebox.askyesno(
            "同じ名前があります",
            f"「{name}」は既にあります。中身を空にして作り直しますか。",
            parent=self,
        ):
            return

        self._scenario = Scenario(name=name)
        self._selected = -1
        self._refresh_steps()
        self._form.show(None)
        self._on_save()

    def _on_rename(self) -> None:
        old = self._scenario.name
        name = simpledialog.askstring(
            "名前を変更", "新しい名前", initialvalue=old, parent=self
        )
        if not name or name == old:
            return

        if storage.exists(name) and not messagebox.askyesno(
            "同じ名前があります",
            f"「{name}」は既にあります。上書きしますか。",
            parent=self,
        ):
            return

        try:
            self._scenario.name = name
            storage.save(self._scenario)
            if old:
                storage.delete(old)
        except ScenarioError as exc:
            self._scenario.name = old
            messagebox.showerror("エラー", str(exc), parent=self)
            return

        # 登録済みのスケジュールは古い名前のまま残ってしまうので付け替える
        self._move_schedule(old, name)
        self._saved_state = self._scenario.to_dict()
        self._reload_scenario_list(select=name)
        self._status_var.set(f"「{name}」に名前を変更しました")

    def _on_save(self) -> None:
        self._form.collect()
        try:
            path = storage.save(self._scenario)
        except ScenarioError as exc:
            messagebox.showerror("エラー", str(exc), parent=self)
            return

        self._saved_state = self._scenario.to_dict()
        self._reload_scenario_list(select=self._scenario.name)
        self._refresh_steps()
        self._status_var.set(f"保存しました: {path.name}")

        problems = self._scenario.validate()
        if problems:
            self._append_log("warn", "未入力の項目があります:")
            for problem in problems:
                self._append_log("warn", f"  {problem}")

    def _on_delete(self) -> None:
        name = self._scenario.name
        if not messagebox.askyesno(
            "確認", f"シナリオ「{name}」を削除しますか。", parent=self
        ):
            return
        try:
            storage.delete(name)
        except ScenarioError as exc:
            messagebox.showerror("エラー", str(exc), parent=self)
            return

        # 消し忘れると、実体の無いシナリオを毎月起動しては失敗し続ける
        removed = self._drop_schedule(name)
        if removed:
            self._append_log(
                "info", f"「{name}」のスケジュール {removed} 件も解除しました。"
            )

        self._scenario = Scenario(name="新しいシナリオ")
        self._selected = -1
        self._saved_state = self._scenario.to_dict()
        self._scenario_var.set("")
        self._reload_scenario_list()
        self._refresh_steps()
        self._form.show(None)
        self._status_var.set(f"「{name}」を削除しました")

    def _drop_schedule(self, name: str) -> int:
        """シナリオに紐づくスケジュールを解除する。消せた件数を返す。

        schtasks は数秒かかることがあるが、削除の直後に呼ぶだけなので
        画面を止めてでも確実に消す（残すほうが害が大きい）。
        """
        try:
            return schedule.unregister_all(name)
        except schedule.ScheduleError as exc:
            messagebox.showerror(
                "スケジュールを解除できませんでした",
                f"「{name}」のスケジュールが残っています。\n"
                f"［スケジュール...］の一覧から削除してください。\n\n{exc}",
                parent=self,
            )
            return 0

    def _move_schedule(self, old: str, new: str) -> None:
        """登録済みのスケジュールを新しい名前に付け替える。"""
        try:
            tasks = [
                task for task in schedule.list_tasks() if task.scenario == old
            ]
            for task in tasks:
                if task.day is None or not task.time_of_day:
                    continue
                schedule.register_monthly(
                    new,
                    day=task.day,
                    time_of_day=task.time_of_day,
                    dry_run=task.dry_run,
                )
            if tasks:
                schedule.unregister_all(old)
                self._append_log(
                    "info", f"スケジュール {len(tasks)} 件を「{new}」に付け替えました。"
                )
        except schedule.ScheduleError as exc:
            messagebox.showerror(
                "スケジュールを付け替えられませんでした",
                f"「{old}」のスケジュールが残っています。\n"
                f"［スケジュール...］の一覧から削除して、登録し直してください。"
                f"\n\n{exc}",
                parent=self,
            )

    def _on_schedule(self) -> None:
        if not self._scenario.name.strip():
            messagebox.showinfo("スケジュール", "先にシナリオを保存してください。",
                                parent=self)
            return
        ScheduleDialog(self, self._scenario.name)

    # ------------------------------------------------------------------
    # 手順
    # ------------------------------------------------------------------
    def _popup_add_menu(self) -> None:
        """追加メニューをボタンの真下に出す。"""
        button = self._add_button
        x = button.winfo_rootx()
        y = button.winfo_rooty() + button.winfo_height()
        try:
            self._add_menu.tk_popup(x, y)
        finally:
            self._add_menu.grab_release()

    def _refresh_steps(self) -> None:
        selection = self._selected
        self._steps.delete(0, tk.END)

        for index, step in enumerate(self._scenario.steps, start=1):
            mark = "" if step.enabled else "（無効）"
            problems = step.validate()
            flag = " ⚠" if problems else ""
            self._steps.insert(tk.END, f" {index:>2}. {step.describe()}{mark}{flag}")
            if problems:
                self._steps.itemconfigure(index - 1, foreground="#b7791f")

        if 0 <= selection < self._steps.size():
            self._steps.selection_set(selection)
            self._selected = selection

        # 空のときは案内を一覧の上に重ねる
        if self._scenario.steps:
            self._empty_hint.grid_remove()
        else:
            self._empty_hint.grid(row=1, column=0, sticky="nsew")
            self._empty_hint.lift()

    def _on_step_selected(self) -> None:
        selection = self._steps.curselection()
        if not selection:
            return
        self._form.collect()
        self._selected = selection[0]
        self._form.show(self._scenario.steps[self._selected])

    def _on_add_step(self, action: str) -> None:
        self._form.collect()
        step = Step(action=action)
        for field in step.spec.fields:
            if field.default is not None:
                step.params[field.key] = field.default

        insert_at = self._selected + 1 if self._selected >= 0 else len(
            self._scenario.steps
        )
        self._scenario.steps.insert(insert_at, step)
        self._selected = insert_at
        self._refresh_steps()
        self._form.show(step)

    def _on_delete_step(self) -> None:
        if self._selected < 0:
            return
        del self._scenario.steps[self._selected]
        self._selected = min(self._selected, len(self._scenario.steps) - 1)
        self._refresh_steps()
        self._form.show(
            self._scenario.steps[self._selected] if self._selected >= 0 else None
        )

    def _on_duplicate(self) -> None:
        if self._selected < 0:
            return
        self._form.collect()
        source = self._scenario.steps[self._selected]
        copy = Step(action=source.action, params=dict(source.params))
        self._scenario.steps.insert(self._selected + 1, copy)
        self._selected += 1
        self._refresh_steps()
        self._form.show(copy)

    def _move(self, delta: int) -> None:
        target = self._selected + delta
        if self._selected < 0 or not 0 <= target < len(self._scenario.steps):
            return
        self._form.collect()
        steps = self._scenario.steps
        steps[self._selected], steps[target] = steps[target], steps[self._selected]
        self._selected = target
        self._refresh_steps()

    def _on_move_up(self) -> None:
        self._move(-1)

    def _on_move_down(self) -> None:
        self._move(1)

    # ------------------------------------------------------------------
    # 実行
    # ------------------------------------------------------------------
    def _on_run(self) -> None:
        self._start_run(dry_run=False)

    def _on_run_dry(self) -> None:
        self._start_run(dry_run=True)

    def _on_run_upto(self) -> None:
        if self._selected < 0:
            messagebox.showinfo(
                "ここまで試し実行", "左の一覧で、どこまで動かすかを選んでください。",
                parent=self,
            )
            return
        self._start_run(dry_run=False, upto=self._selected + 1)

    def _start_run(self, *, dry_run: bool, upto: int | None = None) -> None:
        if self._busy:
            return

        self._form.collect()
        problems = self._scenario.validate()
        if problems:
            messagebox.showerror(
                "実行できません", "\n".join(problems[:10]), parent=self
            )
            return

        self._clear_log()
        self._set_busy(True)
        self._status_var.set("確認実行中..." if dry_run else "実行中...")

        runner = Runner(
            self._scenario,
            storage.work_root(),
            on_event=lambda kind, text: self._queue.put(
                (self._on_log_event, (kind, text))
            ),
            dry_run=dry_run,
        )
        self._runner = runner

        def worker() -> None:
            try:
                report = runner.run(upto=upto)
            except Exception as exc:  # noqa: BLE001 - 想定外は画面に出す
                self._queue.put((self._on_run_error, exc))
            else:
                self._queue.put((self._on_run_done, report))

        threading.Thread(target=worker, name="win_rpa-run", daemon=True).start()

    def _on_cancel(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
            self._status_var.set("中止しています...")

    def _on_run_done(self, report: RunReport) -> None:
        self._set_busy(False)
        self._runner = None

        if report.ok:
            self._status_var.set("完了しました。")
            return

        failed = report.failed
        self._status_var.set(f"手順 {failed.index} で失敗しました。")
        message = f"手順 {failed.index}（{failed.step.describe()}）\n\n{failed.message}"
        if failed.screenshot is not None:
            message += f"\n\n失敗時の画面: {failed.screenshot}"
        messagebox.showerror("実行に失敗しました", message, parent=self)

    def _on_run_error(self, exc: Exception) -> None:
        self._set_busy(False)
        self._runner = None
        self._status_var.set("エラーが発生しました。")
        self._append_log("error", f"想定外のエラー: {exc}")
        messagebox.showerror("エラー", str(exc), parent=self)

    def _on_log_event(self, payload: tuple[str, str]) -> None:
        kind, text = payload
        self._append_log(kind, text)

    # ------------------------------------------------------------------
    # ログ
    # ------------------------------------------------------------------
    def _append_log(self, kind: str, text: str) -> None:
        self._log.configure(state="normal")
        self._log.insert(tk.END, text + "\n", kind)
        self._log.see(tk.END)
        self._log.configure(state="disabled")

    def _clear_log(self) -> None:
        self._log.configure(state="normal")
        self._log.delete("1.0", tk.END)
        self._log.configure(state="disabled")

    # ------------------------------------------------------------------
    # 状態
    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in (self._run_button, self._dry_button, self._upto_button):
            button.configure(state=state)
        self._cancel_button.configure(state="normal" if busy else "disabled")

    def _drain_queue(self) -> None:
        while True:
            try:
                callback, payload = self._queue.get_nowait()
            except queue.Empty:
                return
            callback(payload)

    def _tick(self) -> None:
        self._drain_queue()
        self._tick_id = self.after(_TICK_MS, self._tick)

    # ------------------------------------------------------------------
    def confirm_close(self) -> bool:
        """ウィンドウを閉じてよいか確かめる。main.py から呼ぶ。"""
        if self._busy and not messagebox.askyesno(
            "実行中です",
            "実行中の手順があります。中止して閉じますか。",
            parent=self,
        ):
            return False
        return self._confirm_discard()

    def shutdown(self) -> None:
        """ウィンドウを閉じるときに呼ぶ。定期処理とスレッドを確実に止める。"""
        if self._runner is not None:
            self._runner.cancel()
        if self._tick_id is not None:
            self.after_cancel(self._tick_id)
            self._tick_id = None
