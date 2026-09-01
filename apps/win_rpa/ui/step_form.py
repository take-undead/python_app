"""選択中の手順を編集するパネル。

アクションごとに専用フォームを書かず、ACTIONS の宣言から組み立てる。
アクションを増やしても、このファイルは変わらない。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from logic.actions import ACTIONS, ActionSpec, Step, actions_by_category
from ui.field_editors import EditorContext, FieldEditor, make_editor

# 表示名 → アクション名
_LABEL_TO_ACTION = {spec.label: name for name, spec in ACTIONS.items()}

# ホイールを自分で使う入力欄。ここにパネルのスクロールを繋ぐと、
# 一覧を選び直したり数字が増えたりしてしまうので繋がない
_NO_WHEEL = frozenset(
    {"TCombobox", "TSpinbox", "Spinbox", "Entry", "TEntry", "Text", "Listbox"}
)

# フォルダを基準にする項目。この種類を持つ手順にだけ「今の場所」を出す
# （「ボタンを押す」に保存先を出しても読み飛ばすだけなので）
_USES_FOLDER = frozenset(
    {"folder", "path", "save_path", "file_pattern", "file_name", "record_file"}
)

# 種類の一覧。［＋ 手順を追加］と同じ並び（分類ごと）にしておく。
# 入れ子にできないぶん、せめて並びを揃えて探す場所を一致させる
_ACTION_LABELS = [
    spec.label for _, items in actions_by_category() for _, spec in items
]


class StepForm(ttk.Frame):
    """手順 1 つ分の編集フォーム。"""

    def __init__(
        self,
        master: tk.Misc,
        context: EditorContext,
        on_changed: Callable[[], None],
    ) -> None:
        super().__init__(master, padding=(12, 8))
        self._context = context
        self._on_changed = on_changed

        self._step: Step | None = None
        self._editors: dict[str, FieldEditor] = {}
        self._building = False

        self._action_var = tk.StringVar()
        self._help_var = tk.StringVar()
        self._where_var = tk.StringVar()

        self._build_widgets()
        self.show(None)

    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        self.columnconfigure(1, weight=1)

        ttk.Label(self, text="種類", width=14).grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        self._action_box = ttk.Combobox(
            self,
            textvariable=self._action_var,
            values=_ACTION_LABELS,
            state="readonly",
        )
        self._action_box.grid(row=0, column=1, sticky="ew", pady=(0, 4))
        self._action_var.trace_add("write", lambda *_: self._on_action_changed())

        self._spec_help = ttk.Label(
            self, textvariable=self._help_var, foreground="#666666", wraplength=420
        )
        self._spec_help.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))

        # 手前の「保存先フォルダを選ぶ」「フォルダを作る」で場所が切り替わる。
        # どこを基準に動くのかが読めないと、ファイル名だけ書いた項目が
        # どこに出来るのか分からない
        self._where = ttk.Label(
            self,
            textvariable=self._where_var,
            foreground="#1f6feb",
            wraplength=420,
        )
        self._where.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 6))

        ttk.Separator(self, orient="horizontal").grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8)
        )

        # 項目が多い手順（「ファイルをコピー／移動する」は 8 項目ある）だと、
        # ウィンドウを小さくしたときに下の項目が見切れて触れなくなる。
        # 「種類」と説明は動かさず、項目のところだけを縦にスクロールさせる
        self._area = ttk.Frame(self)
        self._area.grid(row=4, column=0, columnspan=2, sticky="nsew")
        self._area.columnconfigure(0, weight=1)
        self._area.rowconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        self._canvas = tk.Canvas(
            self._area,
            highlightthickness=0,
            borderwidth=0,
            # ttk の枠と地の色を合わせる。既定のままだと白い帯になる
            background=ttk.Style().lookup("TFrame", "background"),
        )
        self._canvas.grid(row=0, column=0, sticky="nsew")

        self._scroll = ttk.Scrollbar(
            self._area, orient="vertical", command=self._canvas.yview
        )
        self._canvas.configure(yscrollcommand=self._scroll.set)

        self._fields = ttk.Frame(self._canvas)
        self._fields.columnconfigure(1, weight=1)
        self._window = self._canvas.create_window(
            (0, 0), window=self._fields, anchor="nw"
        )

        self._fields.bind("<Configure>", lambda _e: self._sync_scroll())
        self._canvas.bind("<Configure>", self._on_canvas_resized)
        self._canvas.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Configure>", self._on_form_resized)

        self._empty = ttk.Label(
            self,
            text="左の一覧から手順を選ぶと、ここで設定できます。",
            foreground="#888888",
        )

    # ------------------------------------------------------------------
    # スクロール
    # ------------------------------------------------------------------
    def _sync_scroll(self) -> None:
        """入りきらないときだけスクロールバーを出す。"""
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        if self._fields.winfo_reqheight() > self._canvas.winfo_height():
            self._scroll.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        else:
            self._scroll.grid_remove()
            self._canvas.yview_moveto(0)

    def _on_canvas_resized(self, event: tk.Event) -> None:
        # 中の枠を横幅いっぱいに広げる。これをしないと columnconfigure の
        # weight が効かず、入力欄が中身の幅のままになる
        self._canvas.itemconfigure(self._window, width=event.width)
        self._sync_scroll()

    def _on_form_resized(self, _event: tk.Event) -> None:
        self._apply_wraplength()

    def _apply_wraplength(self) -> None:
        """説明文の折り返し幅を、今のパネル幅に合わせる。

        固定幅のままだと、パネルを狭めたときに説明が右に見切れる。
        項目の中の「→ ここに出来ます」なども対象なので、エディタごとに
        持たせず、折り返す設定になっているラベルをまとめて拾う。
        """
        width = max(self.winfo_width() - 40, 200)
        self._spec_help.configure(wraplength=width)
        self._where.configure(wraplength=width)
        # 項目の説明は見出しの右に置くので、見出しのぶんを引く
        self._rewrap(self._fields, max(width - 120, 160))

    def _rewrap(self, widget: tk.Misc, width: int) -> None:
        for child in widget.winfo_children():
            try:
                current = int(child.cget("wraplength"))
            except (tk.TclError, ValueError):
                current = 0
            if current:
                child.configure(wraplength=width)
            self._rewrap(child, width)

    def _on_wheel(self, event: tk.Event) -> str | None:
        if self._fields.winfo_reqheight() <= self._canvas.winfo_height():
            return None
        self._canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _bind_wheel(self, widget: tk.Misc) -> None:
        """項目の上でもホイールでスクロールできるようにする。

        入力欄はホイールを自分で使うので繋がない（_NO_WHEEL）。
        """
        if widget.winfo_class() not in _NO_WHEEL:
            widget.bind("<MouseWheel>", self._on_wheel)
        for child in widget.winfo_children():
            self._bind_wheel(child)

    # ------------------------------------------------------------------
    def show(self, step: Step | None) -> None:
        """選択中の手順を表示する。"""
        self._step = step

        if step is None:
            self._action_box.grid_remove()
            self._area.grid_remove()
            self._where.grid_remove()
            self._help_var.set("")
            self._empty.grid(row=0, column=0, columnspan=2, sticky="w")
            return

        self._empty.grid_remove()
        self._action_box.grid()
        self._area.grid()

        self._building = True
        self._action_var.set(step.spec.label)
        self._building = False
        self._build_fields()

    def _on_action_changed(self) -> None:
        if self._building or self._step is None:
            return

        action = _LABEL_TO_ACTION.get(self._action_var.get())
        if action is None or action == self._step.action:
            return

        # 種類を変えても、同じ名前の項目は引き継ぐ（待ち時間や完了条件など）
        self.collect()
        self._step.action = action
        keys = {field.key for field in ACTIONS[action].fields}
        self._step.params = {
            key: value for key, value in self._step.params.items() if key in keys
        }
        self._build_fields()
        self._on_changed()

    def _show_where(self, spec: ActionSpec) -> None:
        """この手順の時点で効いているフォルダを出す。

        手前に「保存先フォルダを選ぶ」「フォルダを作る」があると場所が
        切り替わる。ファイル名だけを書いた項目がどこに出来るのか、
        ［参照...］がどこから始まるのかが、これを見れば分かる。
        """
        if not any(field.kind in _USES_FOLDER for field in spec.fields):
            self._where.grid_remove()
            return

        work_dir, created = self._context.folders_here()
        text = f"この手順の場所: {work_dir}"
        if created is not None and created != work_dir:
            text += f"（作ったフォルダ: {created}）"
        self._where_var.set(text)
        self._where.grid()

    def _build_fields(self) -> None:
        for widget in self._fields.winfo_children():
            widget.destroy()
        self._editors.clear()

        if self._step is None:
            return

        spec = self._step.spec
        self._help_var.set(spec.help)
        self._show_where(spec)

        for row, field in enumerate(spec.fields):
            # bool は自前でラベルを持つので、左の見出しは出さない
            if field.kind != "bool":
                label = field.label + ("　*" if field.required else "")
                ttk.Label(self._fields, text=label, width=14).grid(
                    row=row * 2, column=0, sticky="nw", pady=4
                )

            editor = make_editor(self._fields, field, self._context)
            editor.grid(
                row=row * 2,
                column=0 if field.kind == "bool" else 1,
                columnspan=2 if field.kind == "bool" else 1,
                sticky="ew",
                pady=4,
            )
            self._editors[field.key] = editor

            value = self._step.params.get(field.key, field.default)
            if value is not None:
                editor.set(value)

            if field.help:
                ttk.Label(
                    self._fields,
                    text=field.help,
                    foreground="#888888",
                    wraplength=400,
                ).grid(row=row * 2 + 1, column=1, sticky="w")

        # 別の手順から切り替えたとき、前の位置に留まっていると
        # 上の項目が隠れたまま出る
        self._canvas.yview_moveto(0)
        self._bind_wheel(self._fields)
        self._apply_wraplength()

    # ------------------------------------------------------------------
    def collect(self) -> None:
        """画面の入力値を、編集中の Step に書き戻す。"""
        if self._step is None:
            return
        for key, editor in self._editors.items():
            self._step.params[key] = editor.get()
