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

from logic import appinfo, folder_dialog, schedule, storage
from logic.actions import (
    ACTIONS,
    Scenario,
    ScenarioError,
    Step,
    actions_by_category,
    build_variables,
    requirement_error,
)
from logic.picker import ElementRef
from logic.runner import RunReport, Runner
from ui.about_dialog import AboutDialog
from ui.app_chooser import AppChooser
from ui.group_chooser import GroupChooser
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
        # フォルダ選択は別スレッドで開くので、二重に開かないよう見張る
        self._folder_dialog_open = False
        self._folder_dialog_hwnd = 0

        self._scenario = Scenario(name="新しいシナリオ")
        self._selected = -1
        # グループの見出しを選んでいるときの名前（手順は選ばれていない）
        self._selected_group = ""
        # 閉じているグループ。折りたたみは画面の状態なので保存しない
        self._collapsed: set[str] = set()
        # 保存時点の内容。未保存の編集があるかの判定に使う
        self._saved_state: dict[str, Any] = self._scenario.to_dict()

        self._status_var = tk.StringVar(value="準備完了")
        self._scenario_var = tk.StringVar()

        self._build_widgets()
        self._tick()
        self._refresh_steps()   # 手順 0 件のときの案内を最初から出すため
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

        # 右端に版を出す。まだ作成中であることを、画面を見ただけで
        # 分かるようにしておく（押すと用途と注意点の小窓が開く）
        self._version_label = ttk.Label(
            bar,
            text=appinfo.short_label(),
            foreground="#b7791f" if appinfo.IS_BETA else "#888888",
            cursor="hand2",
        )
        self._version_label.grid(row=0, column=4, sticky="e", padx=(16, 0))
        self._version_label.bind("<Button-1>", lambda _e: self._on_about())

        # 押せると分かるように、乗せたら下線を引く
        base = ("Meiryo UI", 9)
        self._version_label.bind(
            "<Enter>",
            lambda _e: self._version_label.configure(font=(*base, "underline")),
        )
        self._version_label.bind(
            "<Leave>", lambda _e: self._version_label.configure(font=base)
        )
        self._version_label.configure(font=base)

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

        # 操作の種類は増え続けるので、平らに並べず分類ごとの入れ子にする。
        # 分類は logic/actions.py 側に書いてあり、ここは組み立てるだけ
        self._add_menu = tk.Menu(self, tearoff=False)
        self._add_submenus: list[tk.Menu] = []
        # 前提を満たしていない項目を出す前に無効化するので、場所を覚えておく
        self._add_entries: list[tuple[tk.Menu, int, str]] = []
        for category, items in actions_by_category():
            submenu = tk.Menu(self._add_menu, tearoff=False)
            for position, (action, spec) in enumerate(items):
                submenu.add_command(
                    label=spec.label, command=lambda a=action: self._on_add_step(a)
                )
                self._add_entries.append((submenu, position, action))
            self._add_menu.add_cascade(label=category, menu=submenu)
            self._add_submenus.append(submenu)

        self._add_button = ttk.Button(
            header,
            text="＋ 手順を追加  ▾",
            style="Add.TButton",
            command=self._popup_add_menu,
        )
        self._add_button.grid(row=0, column=1, sticky="e")

        # 手順はグループで束ねられる（見出しだけで、実行順は上から 1 本のまま）。
        # 複数選んでまとめて束ねたいので extended にする
        self._steps = ttk.Treeview(left, show="tree", selectmode="extended")
        self._steps.column("#0", width=360, stretch=True)
        self._steps.grid(row=1, column=0, sticky="nsew")
        self._steps.bind("<<TreeviewSelect>>", lambda _e: self._on_step_selected())
        self._steps.bind("<<TreeviewOpen>>", lambda _e: self._on_group_toggled(True))
        self._steps.bind("<<TreeviewClose>>", lambda _e: self._on_group_toggled(False))

        self._steps.tag_configure("group", font=("Meiryo UI", 10, "bold"))
        self._steps.tag_configure("step", font=("Meiryo UI", 10))
        self._steps.tag_configure("warn", foreground="#b7791f")
        self._steps.tag_configure("off", foreground="#999999")

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

        self._group_menu = tk.Menu(self, tearoff=False)
        self._group_menu.add_command(
            label="選んだ手順をまとめる...", command=self._on_group_steps
        )
        self._group_menu.add_command(
            label="別のグループへ移す...", command=self._on_move_to_group
        )
        self._group_menu.add_command(
            label="グループから出す", command=self._on_ungroup_steps
        )
        self._group_menu.add_separator()
        self._group_menu.add_command(
            label="グループ名を変更...", command=self._on_rename_group
        )
        self._group_menu.add_separator()
        self._group_menu.add_command(
            label="すべて折りたたむ", command=lambda: self._set_all_collapsed(True)
        )
        self._group_menu.add_command(
            label="すべて展開する", command=lambda: self._set_all_collapsed(False)
        )

        # ttk.Menubutton は自前で ▾ を描くので、文字には入れない
        self._group_button = ttk.Menubutton(
            controls, text="グループ", menu=self._group_menu, width=10
        )
        self._group_button.grid(row=0, column=5, sticky="e", padx=1)

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

    def choose_folder(
        self, initial: Path, title: str, on_chosen: Callable[[Path], None]
    ) -> None:
        """フォルダ選択を開く。

        tkinter の askdirectory は使えない（pywinauto が COM を MTA に
        するため固まる）。専用スレッドを STA で立てて開き、結果は
        いつもどおり queue 経由でメインスレッドに戻す。
        """
        if self._folder_dialog_open:
            return
        self._folder_dialog_open = True
        self._folder_dialog_hwnd = 0

        def opened(hwnd: int) -> None:
            # ワーカースレッドから呼ばれる。閉じるときに片付けるため覚えるだけ
            self._folder_dialog_hwnd = hwnd

        def worker() -> None:
            try:
                chosen = folder_dialog.choose_folder(initial, title, opened)
            except folder_dialog.FolderDialogError as exc:
                self._queue.put((self._on_folder_error, exc))
            else:
                self._queue.put((self._on_folder_chosen, (chosen, on_chosen)))

        threading.Thread(
            target=worker, name="win_rpa-folder", daemon=True
        ).start()

    def _on_folder_chosen(
        self, payload: tuple[Path | None, Callable[[Path], None]]
    ) -> None:
        chosen, on_chosen = payload
        self._folder_dialog_open = False
        self._folder_dialog_hwnd = 0
        if chosen is None:
            return
        on_chosen(chosen)
        self._refresh_steps()

    def _on_folder_error(self, exc: Exception) -> None:
        self._folder_dialog_open = False
        self._folder_dialog_hwnd = 0
        messagebox.showerror("フォルダを選べませんでした", str(exc), parent=self)

    def work_dir(self) -> Path:
        return self._scenario.resolved_work_dir(storage.work_root())

    def variables(self) -> dict[str, str]:
        """差し込み変数の今の値。フォルダ名のプレビューに使う。

        実行時に切り替わる保存先までは追えないので、ここで出るのは
        「出発点のフォルダで組んだらこうなる」という目安。
        """
        return build_variables(self.work_dir(), scenario_name=self._scenario.name)

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

    def _reset_selection(self) -> None:
        """シナリオを入れ替えるときに、選択と折りたたみを初期に戻す。"""
        self._selected = -1
        self._selected_group = ""
        self._collapsed.clear()

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

        self._reset_selection()
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
        self._reset_selection()
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
        self._reset_selection()
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

    def _on_about(self) -> None:
        AboutDialog(self)

    def _on_schedule(self) -> None:
        if not self._scenario.name.strip():
            messagebox.showinfo("スケジュール", "先にシナリオを保存してください。",
                                parent=self)
            return
        ScheduleDialog(self, self._scenario.name)

    # ------------------------------------------------------------------
    # 手順
    # ------------------------------------------------------------------
    def _insert_position(self) -> int:
        """次の手順を差し込む位置。"""
        if self._selected >= 0:
            return self._selected + 1
        if self._selected_group:
            block = self._block_of_group(self._selected_group)
            if block:
                return block[1][-1] + 1
        return len(self._scenario.steps)

    def _refresh_add_menu_states(self) -> None:
        """前に必要な手順が無い操作を、選べない状態にする。

        選べてしまうと、実行するまで成り立たないことに気づけない。
        理由をラベルに入れて、なぜ押せないかをその場で分かるようにする。
        """
        earlier = self._scenario.steps[: self._insert_position()]

        for menu, position, action in self._add_entries:
            spec = ACTIONS[action]
            problem = requirement_error(spec, earlier)
            menu.entryconfigure(
                position,
                state="disabled" if problem else "normal",
                label=f"{spec.label}（{problem}）" if problem else spec.label,
            )

    def _popup_add_menu(self) -> None:
        """追加メニューをボタンの真下に出す。"""
        self._refresh_add_menu_states()

        button = self._add_button
        x = button.winfo_rootx()
        y = button.winfo_rooty() + button.winfo_height()
        try:
            self._add_menu.tk_popup(x, y)
        finally:
            self._add_menu.grab_release()

    def _refresh_steps(self) -> None:
        """一覧を組み直す。グループは見出しの親ノードとして出す。"""
        selection = self._selected
        selected_group = self._selected_group

        self._steps.delete(*self._steps.get_children())

        for block, (group, indices) in enumerate(self._scenario.groups()):
            parent = ""
            if group:
                parent = f"g{block}"
                self._steps.insert(
                    "",
                    "end",
                    iid=parent,
                    text=f"　{group}（{len(indices)} 手順）",
                    open=group not in self._collapsed,
                    tags=("group",),
                )

            for index in indices:
                step = self._scenario.steps[index]
                mark = "" if step.enabled else "（無効）"
                problems = self._scenario.step_problems(index)
                flag = " ⚠" if problems else ""
                tags = ["step"]
                if problems:
                    tags.append("warn")
                if not step.enabled:
                    tags.append("off")
                self._steps.insert(
                    parent,
                    "end",
                    iid=f"s{index}",
                    text=f" {index + 1:>2}. {step.describe()}{mark}{flag}",
                    tags=tuple(tags),
                )

        self._restore_selection(selection, selected_group)

        # 空のときは案内を一覧の上に重ねる
        if self._scenario.steps:
            self._empty_hint.grid_remove()
        else:
            self._empty_hint.grid(row=1, column=0, sticky="nsew")
            self._empty_hint.lift()

    def _restore_selection(self, index: int, group: str) -> None:
        """組み直したあとに、選んでいたものを選び直す。"""
        if 0 <= index < len(self._scenario.steps):
            iid = f"s{index}"
            if self._steps.exists(iid):
                self._steps.see(iid)
                self._steps.selection_set(iid)
                self._selected = index
                self._selected_group = ""
                return

        if group:
            for block, (name, _) in enumerate(self._scenario.groups()):
                if name == group and self._steps.exists(f"g{block}"):
                    self._steps.selection_set(f"g{block}")
                    self._selected = -1
                    self._selected_group = group
                    return

        self._selected = -1
        self._selected_group = ""

    def _selected_indices(self) -> list[int]:
        """選ばれている手順の位置を、上から順に返す。

        グループの見出しを選んでいるときは、その中の手順すべてを指す。
        """
        indices: set[int] = set()
        blocks = self._scenario.groups()

        for iid in self._steps.selection():
            if iid.startswith("s"):
                indices.add(int(iid[1:]))
            elif iid.startswith("g"):
                block = int(iid[1:])
                if block < len(blocks):
                    indices.update(blocks[block][1])
        return sorted(indices)

    def _on_step_selected(self) -> None:
        selection = self._steps.selection()
        if not selection:
            return
        self._form.collect()

        iid = selection[0]
        if iid.startswith("g"):
            blocks = self._scenario.groups()
            block = int(iid[1:])
            self._selected = -1
            self._selected_group = blocks[block][0] if block < len(blocks) else ""
            self._form.show(None)
            return

        self._selected = int(iid[1:])
        self._selected_group = ""
        self._form.show(self._scenario.steps[self._selected])

    def _on_group_toggled(self, opened: bool) -> None:
        """折りたたみの状態を覚える。組み直しても畳んだままにするため。"""
        iid = self._steps.focus()
        if not iid.startswith("g"):
            return
        blocks = self._scenario.groups()
        block = int(iid[1:])
        if block >= len(blocks):
            return
        name = blocks[block][0]
        if opened:
            self._collapsed.discard(name)
        else:
            self._collapsed.add(name)

    def _on_add_step(self, action: str) -> None:
        self._form.collect()
        insert_at = self._insert_position()

        # メニューでは無効にしてあるが、経路が増えても素通りしないよう確かめる
        problem = requirement_error(
            ACTIONS[action], self._scenario.steps[:insert_at]
        )
        if problem:
            messagebox.showinfo(
                "この操作はまだ入れられません",
                f"「{ACTIONS[action].label}」は{problem}。\n\n"
                "先にその手順を追加してから、もう一度選んでください。",
                parent=self,
            )
            return

        step = Step(action=action)
        for field in step.spec.fields:
            if field.default is not None:
                step.params[field.key] = field.default

        if self._selected >= 0:
            # 直前の手順と同じグループに入れる。追加のたびに束ね直さなくて済む
            step.group = self._scenario.steps[self._selected].group
        elif self._selected_group:
            step.group = self._selected_group

        self._scenario.steps.insert(insert_at, step)
        self._selected = insert_at
        self._selected_group = ""
        self._refresh_steps()
        self._form.show(step)

    def _on_delete_step(self) -> None:
        indices = self._selected_indices()
        if not indices:
            return

        if len(indices) > 1 and not messagebox.askyesno(
            "確認", f"選んだ {len(indices)} 件の手順を削除しますか。", parent=self
        ):
            return

        for index in reversed(indices):
            del self._scenario.steps[index]

        self._selected = min(indices[0], len(self._scenario.steps) - 1)
        self._selected_group = ""
        self._refresh_steps()
        self._form.show(
            self._scenario.steps[self._selected] if self._selected >= 0 else None
        )

    def _on_duplicate(self) -> None:
        if self._selected < 0:
            return
        self._form.collect()
        source = self._scenario.steps[self._selected]
        copy = Step(
            action=source.action, params=dict(source.params), group=source.group
        )
        self._scenario.steps.insert(self._selected + 1, copy)
        self._selected += 1
        self._refresh_steps()
        self._form.show(copy)

    def _move(self, delta: int) -> None:
        """選んでいる手順（またはグループ）を 1 つ動かす。"""
        if self._selected_group:
            self._move_group(delta)
            return
        if self._selected < 0:
            return

        self._form.collect()
        steps = self._scenario.steps
        source = self._selected
        target = source + delta

        if not 0 <= target < len(steps):
            # 一覧の端。グループに入っていれば、そこから出すだけにする
            if steps[source].group:
                steps[source].group = ""
                self._refresh_steps()
            return

        if steps[target].group != steps[source].group:
            # グループの境目。まず境を越えさせ、次の押下で入れ替える。
            # 1 回で両方やると、境目でグループを移せなくなる
            steps[source].group = steps[target].group
        else:
            steps[source], steps[target] = steps[target], steps[source]
            self._selected = target

        self._refresh_steps()

    def _move_group(self, delta: int) -> None:
        """グループごと、隣の塊と入れ替える。"""
        self._form.collect()
        blocks = self._scenario.groups()
        position = next(
            (i for i, (name, _) in enumerate(blocks) if name == self._selected_group),
            None,
        )
        if position is None:
            return

        other = position + delta
        if not 0 <= other < len(blocks):
            return

        first, second = sorted((position, other))
        steps = self._scenario.steps
        head = blocks[first][1]
        tail = blocks[second][1]
        moved = [steps[i] for i in tail] + [steps[i] for i in head]
        self._scenario.steps = steps[: head[0]] + moved + steps[tail[-1] + 1 :]
        self._refresh_steps()

    def _on_move_up(self) -> None:
        self._move(-1)

    def _on_move_down(self) -> None:
        self._move(1)

    # ------------------------------------------------------------------
    # グループ
    # ------------------------------------------------------------------
    def _block_of_group(self, name: str) -> tuple[str, list[int]] | None:
        return next(
            (block for block in self._scenario.groups() if block[0] == name), None
        )

    def _apply_group(self, indices: list[int], name: str) -> None:
        """選んだ手順を 1 つのグループにし、離れていたら寄せる。"""
        self._form.collect()
        steps = self._scenario.steps
        for index in indices:
            steps[index].group = name

        # 選んだものが飛び飛びでも、先頭の位置に集める
        chosen = set(indices)
        picked = [steps[i] for i in indices]
        rest = [step for i, step in enumerate(steps) if i not in chosen]
        before = sum(1 for i in range(indices[0]) if i not in chosen)

        self._scenario.steps = rest[:before] + picked + rest[before:]
        self._scenario.normalize_groups()

        # 同じ内容の手順が並ぶことがあるので、値ではなく実体で探す
        self._selected = next(
            i for i, step in enumerate(self._scenario.steps) if step is picked[0]
        )
        self._selected_group = ""
        self._refresh_steps()

    def _ask_group_name(self, title: str, initial: str = "") -> str | None:
        name = simpledialog.askstring(
            title, "グループ名", initialvalue=initial, parent=self
        )
        return name.strip() if name else None

    def _on_group_steps(self) -> None:
        indices = self._selected_indices()
        if not indices:
            messagebox.showinfo(
                "グループ", "まとめる手順を一覧で選んでください。", parent=self
            )
            return

        name = self._ask_group_name("選んだ手順をまとめる")
        if not name:
            return
        self._apply_group(indices, name)
        self._status_var.set(f"{len(indices)} 手順を「{name}」にまとめました")

    def _on_move_to_group(self) -> None:
        indices = self._selected_indices()
        if not indices:
            return

        names = self._scenario.group_names()
        if not names:
            self._on_group_steps()
            return

        GroupChooser(self, names, lambda name: self._apply_group(indices, name))

    def _on_ungroup_steps(self) -> None:
        indices = self._selected_indices()
        if not indices:
            return
        self._apply_group(indices, "")
        self._status_var.set(f"{len(indices)} 手順をグループから出しました")

    def _on_rename_group(self) -> None:
        old = self._selected_group
        if not old:
            messagebox.showinfo(
                "グループ名を変更", "一覧でグループの見出しを選んでください。",
                parent=self,
            )
            return

        name = self._ask_group_name("グループ名を変更", old)
        if not name or name == old:
            return

        for step in self._scenario.steps:
            if step.group == old:
                step.group = name

        self._collapsed.discard(old)
        self._scenario.normalize_groups()
        self._selected_group = name
        self._refresh_steps()
        self._status_var.set(f"「{old}」を「{name}」に変更しました")

    def _set_all_collapsed(self, collapsed: bool) -> None:
        if collapsed:
            self._collapsed = set(self._scenario.group_names())
        else:
            self._collapsed.clear()
        self._refresh_steps()

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
        # 開きっぱなしのフォルダ選択を閉じる。放っておくと
        # ダイアログを抱えたスレッドが残ったまま画面だけ消える
        folder_dialog.close_dialog(self._folder_dialog_hwnd)
        self._folder_dialog_hwnd = 0
        if self._tick_id is not None:
            self.after_cancel(self._tick_id)
            self._tick_id = None
