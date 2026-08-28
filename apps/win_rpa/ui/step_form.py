"""選択中の手順を編集するパネル。

アクションごとに専用フォームを書かず、ACTIONS の宣言から組み立てる。
アクションを増やしても、このファイルは変わらない。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from logic.actions import ACTIONS, Step
from ui.field_editors import EditorContext, FieldEditor, make_editor

# 表示名 → アクション名
_LABEL_TO_ACTION = {spec.label: name for name, spec in ACTIONS.items()}


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
            values=[spec.label for spec in ACTIONS.values()],
            state="readonly",
        )
        self._action_box.grid(row=0, column=1, sticky="ew", pady=(0, 4))
        self._action_var.trace_add("write", lambda *_: self._on_action_changed())

        ttk.Label(
            self, textvariable=self._help_var, foreground="#666666", wraplength=420
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))

        ttk.Separator(self, orient="horizontal").grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8)
        )

        self._fields = ttk.Frame(self)
        self._fields.grid(row=3, column=0, columnspan=2, sticky="nsew")
        self._fields.columnconfigure(1, weight=1)
        self.rowconfigure(3, weight=1)

        self._empty = ttk.Label(
            self,
            text="左の一覧から手順を選ぶと、ここで設定できます。",
            foreground="#888888",
        )

    # ------------------------------------------------------------------
    def show(self, step: Step | None) -> None:
        """選択中の手順を表示する。"""
        self._step = step

        if step is None:
            self._action_box.grid_remove()
            self._fields.grid_remove()
            self._help_var.set("")
            self._empty.grid(row=0, column=0, columnspan=2, sticky="w")
            return

        self._empty.grid_remove()
        self._action_box.grid()
        self._fields.grid()

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

    def _build_fields(self) -> None:
        for widget in self._fields.winfo_children():
            widget.destroy()
        self._editors.clear()

        if self._step is None:
            return

        spec = self._step.spec
        self._help_var.set(spec.help)

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

    # ------------------------------------------------------------------
    def collect(self) -> None:
        """画面の入力値を、編集中の Step に書き戻す。"""
        if self._step is None:
            return
        for key, editor in self._editors.items():
            self._step.params[key] = editor.get()
