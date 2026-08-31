"""入力項目のウィジェット。

アクションごとに専用の編集フォームを書くと、種類が増えるたびに UI が
膨らむ。Field.kind を見てここでウィジェットを選ぶことで、アクションを
1 つ増やすコストを 10 行程度に抑える。

ユーザーに文字を打たせない方針なので、要素・アプリ・完了条件・差し込み変数は
すべてボタンかドロップダウンから指定させる。
"""

from __future__ import annotations

import os
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any, Callable, Protocol

from logic import winfind
from logic.actions import (
    DATE_RANGE_LABELS,
    DATE_RANGES,
    FILE_NAME_PATTERNS,
    FILE_PATTERNS,
    FOLDER_PATTERNS,
    KEY_CHOICES,
    KEY_LABELS,
    VARIABLE_LABELS,
    WAIT_KINDS,
    Field,
    clamp_day,
    expand,
    resolve_date_range,
)
from logic.picker import ElementRef, describe as describe_element


class EditorContext(Protocol):
    """エディタが親に頼む操作。"""

    def pick_element(self, on_picked: Callable[[ElementRef], None]) -> None:
        """要素ピッカーを開く。"""

    def choose_app(self, on_chosen: Callable[[dict[str, Any]], None]) -> None:
        """アプリ選択を開く。"""

    def work_dir(self) -> Path:
        """作業フォルダ（ファイル選択の初期位置）。"""

    def variables(self) -> dict[str, str]:
        """差し込み変数の今の値（プレビュー表示に使う）。"""

    def choose_folder(
        self, initial: Path, title: str, on_chosen: Callable[[Path], None]
    ) -> None:
        """フォルダ選択を開く（別スレッドで開き、結果をあとから返す）。"""


class FieldEditor(ttk.Frame):
    """入力項目 1 つ分のウィジェット。"""

    def __init__(
        self, master: tk.Misc, field: Field, context: EditorContext
    ) -> None:
        super().__init__(master)
        self.field = field
        self.context = context
        self.columnconfigure(0, weight=1)
        self._build()

    def _build(self) -> None:  # サブクラスが実装する
        raise NotImplementedError

    def get(self) -> Any:
        raise NotImplementedError

    def set(self, value: Any) -> None:
        raise NotImplementedError


# ----------------------------------------------------------------------
# 差し込み変数
# ----------------------------------------------------------------------
def _attach_variable_menu(parent: tk.Misc, entry: ttk.Entry) -> ttk.Menubutton:
    """「差し込み ▼」ボタンを作る。

    {yyyymm} のような書き方をユーザーに覚えさせない。選ぶと入力欄の
    カーソル位置に挿し込まれる。
    """
    button = ttk.Menubutton(parent, text="差し込み ▼", width=11)
    menu = tk.Menu(button, tearoff=False)

    for key, label in VARIABLE_LABELS.items():
        menu.add_command(
            label=label,
            command=lambda k=key: entry.insert(tk.INSERT, "{" + k + "}"),
        )

    button.configure(menu=menu)
    return button


def _attach_pattern_menu(
    parent: tk.Misc,
    var: tk.StringVar,
    patterns: tuple[tuple[str, str], ...],
    text: str,
) -> ttk.Menubutton:
    """「型 ▼」ボタンを作る。選ぶと欄の中身をその型に置き換える。

    差し込みと違って追記ではなく総取り替えにする。フォルダ名は
    「年月だけ」「年の下に月」のように丸ごと決まることがほとんどで、
    継ぎ足すと打ち間違いの余地が残るため。
    """
    button = ttk.Menubutton(parent, text=text, width=8)
    menu = tk.Menu(button, tearoff=False)

    for value, label in patterns:
        menu.add_command(label=label, command=lambda v=value: var.set(v))

    button.configure(menu=menu)
    return button


# ----------------------------------------------------------------------
# 各 kind
# ----------------------------------------------------------------------
class TextEditor(FieldEditor):
    def _build(self) -> None:
        self._var = tk.StringVar(value=str(self.field.default or ""))
        entry = ttk.Entry(self, textvariable=self._var)
        entry.grid(row=0, column=0, sticky="ew")
        _attach_variable_menu(self, entry).grid(row=0, column=1, padx=(4, 0))

    def get(self) -> Any:
        return self._var.get()

    def set(self, value: Any) -> None:
        self._var.set("" if value is None else str(value))


class IntEditor(FieldEditor):
    def _build(self) -> None:
        self._var = tk.StringVar(value=str(self.field.default or 0))
        ttk.Spinbox(
            self, from_=0, to=86400, textvariable=self._var, width=10
        ).grid(row=0, column=0, sticky="w")

    def get(self) -> Any:
        try:
            return int(self._var.get())
        except ValueError:
            return self.field.default or 0

    def set(self, value: Any) -> None:
        self._var.set(str(value if value is not None else (self.field.default or 0)))


class BoolEditor(FieldEditor):
    def _build(self) -> None:
        self._var = tk.BooleanVar(value=bool(self.field.default))
        ttk.Checkbutton(self, text=self.field.label, variable=self._var).grid(
            row=0, column=0, sticky="w"
        )

    def get(self) -> Any:
        return self._var.get()

    def set(self, value: Any) -> None:
        self._var.set(bool(value))


class ChoiceEditor(FieldEditor):
    def _build(self) -> None:
        self._var = tk.StringVar(value=str(self.field.default or ""))
        ttk.Combobox(
            self,
            textvariable=self._var,
            values=list(self.field.choices),
            state="readonly",
        ).grid(row=0, column=0, sticky="ew")

    def get(self) -> Any:
        return self._var.get()

    def set(self, value: Any) -> None:
        self._var.set("" if value is None else str(value))


class KeysEditor(FieldEditor):
    """押すキー。一覧から選ばせる。

    {ENTER} や ^s という書き方を人に打たせない。画面には「Enter（決定）」の
    ように出し、保存されるのは pywinauto に渡す値のほう。
    """

    def _build(self) -> None:
        self._value = str(self.field.default or "{ENTER}")
        self._var = tk.StringVar(value=KEY_LABELS.get(self._value, self._value))

        self._combo = ttk.Combobox(
            self,
            textvariable=self._var,
            values=[label for _, label in KEY_CHOICES],
            state="readonly",
            width=26,
        )
        self._combo.grid(row=0, column=0, sticky="w")

    def get(self) -> Any:
        label = self._var.get()
        for value, text in KEY_CHOICES:
            if text == label:
                return value
        # 一覧に無い値は、読み込んだときのまま返す（手で足した設定を壊さない）
        return self._value

    def set(self, value: Any) -> None:
        self._value = str(value or "{ENTER}")
        self._var.set(KEY_LABELS.get(self._value, self._value))


class DateRangeEditor(FieldEditor):
    """更新日の範囲。「先月」などの型から選ばせる。

    日付を打たせない。月次で使う言い方（先月・今月・今日）を並べ、
    実際にいつからいつまでになるかをその場に出す。今日動かしたら
    どの範囲になるかが見えないと、選んだものが合っているか分からない。

    どうしても決まった期間を指定したいときだけ、年・月・日を
    スピンボックスで選ばせる（ここも打ち込みではない）。
    """

    def _build(self) -> None:
        self._kind_var = tk.StringVar(value=DATE_RANGE_LABELS["none"])
        self._preview_var = tk.StringVar(value="")

        self.columnconfigure(0, weight=1)
        combo = ttk.Combobox(
            self,
            textvariable=self._kind_var,
            values=[label for _, label in DATE_RANGES],
            state="readonly",
            width=26,
        )
        combo.grid(row=0, column=0, sticky="w")

        # 日付の欄は「期間を指定する」を選んだときだけ出す。
        # 入れ物を先に作ってから中身を作る（別の親に grid すると描かれない）
        self._custom = ttk.Frame(self)
        self._custom.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._from = _DayPicker(self._custom, "開始日")
        self._to = _DayPicker(self._custom, "終了日")
        self._from.grid(row=0, column=0, sticky="w")
        self._to.grid(row=1, column=0, sticky="w", pady=(2, 0))

        ttk.Label(
            self, textvariable=self._preview_var, foreground="#666666",
            wraplength=420,
        ).grid(row=2, column=0, sticky="w", pady=(2, 0))

        self._kind_var.trace_add("write", lambda *_: self._refresh())
        self._from.on_changed = self._refresh
        self._to.on_changed = self._refresh
        self._refresh()

    def _kind(self) -> str:
        label = self._kind_var.get()
        for value, text in DATE_RANGES:
            if text == label:
                return value
        return "none"

    def _refresh(self) -> None:
        kind = self._kind()
        if kind == "custom":
            self._custom.grid()
        else:
            self._custom.grid_remove()

        if kind == "none":
            self._preview_var.set("（すべてのファイルが対象）")
            return

        start, end = resolve_date_range(self.get())
        if start is None or end is None:
            self._preview_var.set("（すべてのファイルが対象）")
            return

        when = "今日動かすと " if kind not in ("custom",) else ""
        self._preview_var.set(
            f"→ {when}{start:%Y-%m-%d} 〜 {end:%Y-%m-%d} に"
            "更新されたファイルだけが対象になります"
        )

    def get(self) -> Any:
        kind = self._kind()
        if kind == "none":
            return {"kind": "none"}
        if kind != "custom":
            return {"kind": kind}
        return {
            "kind": "custom",
            "from": self._from.get().isoformat(),
            "to": self._to.get().isoformat(),
        }

    def set(self, value: Any) -> None:
        data = dict(value or {})
        kind = str(data.get("kind", "none"))
        self._kind_var.set(DATE_RANGE_LABELS.get(kind, DATE_RANGE_LABELS["none"]))
        if kind == "custom":
            self._from.set(data.get("from"))
            self._to.set(data.get("to"))
        self._refresh()


class _DayPicker(ttk.Frame):
    """年・月・日をスピンボックスで選ばせる小さな部品。"""

    def __init__(self, master: tk.Misc, label: str) -> None:
        super().__init__(master)
        self.on_changed: Callable[[], None] = lambda: None

        today = date.today()
        self._year = tk.StringVar(value=str(today.year))
        self._month = tk.StringVar(value=str(today.month))
        self._day = tk.StringVar(value="1")

        ttk.Label(self, text=label, width=7).grid(row=0, column=0, sticky="w")
        for column, (var, to, unit) in enumerate(
            ((self._year, 2100, "年"), (self._month, 12, "月"), (self._day, 31, "日")),
            start=1,
        ):
            ttk.Spinbox(
                self, from_=1 if unit != "年" else 2000, to=to,
                textvariable=var, width=6 if unit == "年" else 4,
                command=lambda: self.on_changed(),
            ).grid(row=0, column=column * 2 - 1, sticky="w", padx=(4, 0))
            ttk.Label(self, text=unit).grid(row=0, column=column * 2, sticky="w")
            var.trace_add("write", lambda *_: self.on_changed())

    def get(self) -> date:
        def number(var: tk.StringVar, fallback: int) -> int:
            try:
                return int(var.get())
            except ValueError:
                return fallback

        return clamp_day(
            number(self._year, date.today().year),
            number(self._month, date.today().month),
            number(self._day, 1),
        )

    def set(self, value: Any) -> None:
        try:
            day = date.fromisoformat(str(value))
        except (TypeError, ValueError):
            return
        self._year.set(str(day.year))
        self._month.set(str(day.month))
        self._day.set(str(day.day))


class PathEditor(FieldEditor):
    """既にあるファイルを選ぶ。"""

    save_mode = False

    def _build(self) -> None:
        self._var = tk.StringVar(value=str(self.field.default or ""))
        entry = ttk.Entry(self, textvariable=self._var)
        entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(self, text="参照...", command=self._browse, width=8).grid(
            row=0, column=1, padx=(4, 0)
        )
        # 開くファイルにも {yyyymm} を使う（前月分を読み込ませる）ため、
        # 保存先かどうかに関わらず差し込みを出す
        _attach_variable_menu(self, entry).grid(row=0, column=2, padx=(4, 0))

    def _browse(self) -> None:
        initial = self.context.work_dir()
        if self.save_mode:
            path = filedialog.asksaveasfilename(
                parent=self, title=self.field.label, initialdir=str(initial),
                defaultextension=".csv",
                filetypes=[("CSV ファイル", "*.csv"), ("すべて", "*.*")],
            )
        else:
            path = filedialog.askopenfilename(
                parent=self, title=self.field.label, initialdir=str(initial)
            )
        if not path:
            return

        # 作業フォルダの中なら相対で持つ。PC が変わっても壊れないようにするため
        chosen = Path(path)
        try:
            chosen = chosen.relative_to(initial)
        except ValueError:
            pass
        self._var.set(str(chosen))

    def get(self) -> Any:
        return self._var.get()

    def set(self, value: Any) -> None:
        self._var.set("" if value is None else str(value))


class SavePathEditor(PathEditor):
    save_mode = True


class FolderEditor(FieldEditor):
    """フォルダ。既にあるものを選ぶか、年月日などの型から名前を組み立てる。

    月次で回すので、毎月同じ名前のフォルダに書き続けるのはまずい。
    かといって {yyyymm} という書き方をユーザーに覚えさせたくないので、
    ［型 ▼］で選ばせ、実際に出来上がる名前をその場に出す。
    """

    def _build(self) -> None:
        self._var = tk.StringVar(value=str(self.field.default or ""))
        self._preview_var = tk.StringVar(value="")

        self.columnconfigure(0, weight=1)
        entry = ttk.Entry(self, textvariable=self._var)
        entry.grid(row=0, column=0, sticky="ew")

        ttk.Button(self, text="参照...", command=self._browse, width=8).grid(
            row=0, column=1, padx=(4, 0)
        )
        _attach_pattern_menu(self, self._var, FOLDER_PATTERNS, "型 ▼").grid(
            row=0, column=2, padx=(4, 0)
        )
        _attach_variable_menu(self, entry).grid(row=0, column=3, padx=(4, 0))

        ttk.Label(
            self, textvariable=self._preview_var, foreground="#666666", wraplength=420
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 0))

        self._var.trace_add("write", lambda *_: self._refresh_preview())
        self._refresh_preview()

    def _browse(self) -> None:
        """フォルダ選択を開く。

        filedialog.askdirectory は使わない（pywinauto が COM を MTA に
        するため固まる）。詳細は logic/folder_dialog.py を参照。
        別スレッドで開くので、結果は _on_chosen にあとから返ってくる。
        """
        self.context.choose_folder(
            self.context.work_dir(), self.field.label, self._on_chosen
        )

    def _on_chosen(self, path: Path) -> None:
        # 選んでいる間に別の手順へ移ることがあるので、生きているか確かめる
        if not self.winfo_exists():
            return

        # 作業フォルダの中なら相対で持つ。PC が変わっても壊れないようにするため
        chosen = path
        try:
            chosen = chosen.relative_to(self.context.work_dir())
        except ValueError:
            pass

        # 保存先フォルダそのものを選ぶと "." になる。意味は「空」と同じなので
        # そちらに寄せる（欄に . が残ると何を指しているか読めない）
        text = str(chosen)
        self._var.set("" if text == "." else text)

    def _refresh_preview(self) -> None:
        raw = self._var.get().strip()
        if not raw:
            self._preview_var.set("（空のまま。今の保存先フォルダを使います）")
            return

        expanded = expand(raw, self.context.variables())
        folder = Path(expanded)
        if not folder.is_absolute():
            folder = self.context.work_dir() / folder
        mark = "" if folder.is_dir() else "   ← まだありません"
        self._preview_var.set(f"→ {folder}{mark}")

    def get(self) -> Any:
        return self._var.get()

    def set(self, value: Any) -> None:
        self._var.set("" if value is None else str(value))


class FileNameEditor(FieldEditor):
    """できあがるファイルの名前。型から選び、今日ならどうなるかを出す。"""

    def _build(self) -> None:
        self._var = tk.StringVar(value=str(self.field.default or ""))
        self._preview_var = tk.StringVar(value="")

        self.columnconfigure(0, weight=1)
        entry = ttk.Entry(self, textvariable=self._var)
        entry.grid(row=0, column=0, sticky="ew")

        _attach_pattern_menu(self, self._var, FILE_NAME_PATTERNS, "型 ▼").grid(
            row=0, column=1, padx=(4, 0)
        )
        _attach_variable_menu(self, entry).grid(row=0, column=2, padx=(4, 0))

        ttk.Label(
            self, textvariable=self._preview_var, foreground="#666666", wraplength=420
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))

        self._var.trace_add("write", lambda *_: self._refresh_preview())
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        raw = self._var.get().strip()
        if not raw:
            self._preview_var.set("（未指定）")
            return

        name = expand(raw, self.context.variables())
        if not name.lower().endswith(".csv"):
            name += ".csv"
        self._preview_var.set(f"→ 今日動かすと {name} ができます")

    def get(self) -> Any:
        return self._var.get()

    def set(self, value: Any) -> None:
        self._var.set("" if value is None else str(value))


class FilePatternEditor(FieldEditor):
    """まとめて扱うファイルの選び方。

    「すべて」「CSV すべて」を選ぶだけで済ませられるようにし、
    今そこに何件あるかを出す。0 件のまま気づかず組んでしまうのを防ぐ。
    """

    def _build(self) -> None:
        self._var = tk.StringVar(value=str(self.field.default or "*.csv"))
        self._preview_var = tk.StringVar(value="")

        self.columnconfigure(0, weight=1)
        entry = ttk.Entry(self, textvariable=self._var)
        entry.grid(row=0, column=0, sticky="ew")

        ttk.Button(self, text="1 つ選ぶ...", command=self._browse, width=10).grid(
            row=0, column=1, padx=(4, 0)
        )
        _attach_pattern_menu(self, self._var, FILE_PATTERNS, "型 ▼").grid(
            row=0, column=2, padx=(4, 0)
        )
        _attach_variable_menu(self, entry).grid(row=0, column=3, padx=(4, 0))

        ttk.Label(
            self, textvariable=self._preview_var, foreground="#666666", wraplength=420
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 0))

        self._var.trace_add("write", lambda *_: self._refresh_preview())
        self._refresh_preview()

    def _browse(self) -> None:
        initial = self.context.work_dir()
        path = filedialog.askopenfilename(
            parent=self, title=self.field.label, initialdir=str(initial)
        )
        if path:
            self._var.set(Path(path).name)

    def _refresh_preview(self) -> None:
        pattern = expand(self._var.get().strip(), self.context.variables())
        folder = self.context.work_dir()

        if not pattern:
            self._preview_var.set("（未指定）")
            return
        if not folder.is_dir():
            self._preview_var.set(f"{folder} はまだありません")
            return

        try:
            hits = sorted(p.name for p in folder.glob(pattern) if p.is_file())
        except (NotImplementedError, ValueError, OSError):
            # フルパスを入れると glob が投げる。打ち込み途中でも出るので
            # 例外にせず、そのまま案内だけ差し替える
            self._preview_var.set("この書き方では探せません（［型 ▼］から選べます）")
            return

        if not hits:
            self._preview_var.set(f"{folder} には今 0 件です")
            return

        shown = "、".join(hits[:3]) + ("…" if len(hits) > 3 else "")
        self._preview_var.set(f"今 {len(hits)} 件: {shown}")

    def get(self) -> Any:
        return self._var.get()

    def set(self, value: Any) -> None:
        self._var.set("" if value is None else str(value))


class WindowTitleEditor(FieldEditor):
    """ウィンドウの題名。今開いているものから選ばせる。

    題名は完全一致で照合するので、打ち間違えると永久に見つからない手順が
    できてしまう。一覧から選べるようにしておく。

    ただし「これから出るダイアログ」を先に指定したい場面もあるため、
    入力もできる状態にしておく。
    """

    def _build(self) -> None:
        self._var = tk.StringVar(value=str(self.field.default or ""))

        self._combo = ttk.Combobox(self, textvariable=self._var)
        self._combo.grid(row=0, column=0, sticky="ew")

        ttk.Button(self, text="更新", command=self._reload, width=6).grid(
            row=0, column=1, padx=(4, 0)
        )
        ttk.Label(
            self,
            text="今開いているウィンドウから選べます（一覧に無ければ直接入力）",
            foreground="#888888",
        ).grid(row=1, column=0, columnspan=2, sticky="w")

        self._reload()

    def _reload(self) -> None:
        try:
            titles = winfind.list_window_titles(exclude_pid=os.getpid())
        except OSError:
            titles = []
        self._combo.configure(values=titles)

    def get(self) -> Any:
        return self._var.get()

    def set(self, value: Any) -> None:
        self._var.set("" if value is None else str(value))


class ElementEditor(FieldEditor):
    """ピッカーで採取した要素。識別子は画面に出さない。"""

    def _build(self) -> None:
        self._ref: ElementRef | None = None
        self._var = tk.StringVar(value="（未指定）")

        ttk.Label(self, textvariable=self._var, font=("Meiryo UI", 10, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(self, text="要素を選ぶ...", command=self._pick, width=14).grid(
            row=0, column=1, padx=(8, 0)
        )
        self._where_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self._where_var, foreground="#666666").grid(
            row=1, column=0, columnspan=2, sticky="w"
        )

    def _pick(self) -> None:
        self.context.pick_element(self._on_picked)

    def _on_picked(self, ref: ElementRef) -> None:
        self._ref = ref
        self._refresh()

    def _refresh(self) -> None:
        if self._ref is None:
            self._var.set("（未指定）")
            self._where_var.set("")
            return
        self._var.set(describe_element(self._ref))
        self._where_var.set(f"ウィンドウ: {self._ref.window_title or '（不明）'}")

    def get(self) -> Any:
        return self._ref.to_dict() if self._ref is not None else None

    def set(self, value: Any) -> None:
        self._ref = ElementRef.from_dict(value) if value else None
        self._refresh()


class AppEditor(FieldEditor):
    """ショートカット一覧から選んだアプリ。"""

    def _build(self) -> None:
        self._params: dict[str, Any] | None = None
        self._var = tk.StringVar(value="（未指定）")
        self._path_var = tk.StringVar(value="")

        ttk.Label(self, textvariable=self._var, font=("Meiryo UI", 10, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(self, text="アプリを選ぶ...", command=self._choose, width=14).grid(
            row=0, column=1, padx=(8, 0)
        )
        ttk.Label(
            self, textvariable=self._path_var, foreground="#666666", wraplength=380
        ).grid(row=1, column=0, columnspan=2, sticky="w")

    def _choose(self) -> None:
        self.context.choose_app(self._on_chosen)

    def _on_chosen(self, params: dict[str, Any]) -> None:
        self._params = params
        self._refresh()

    def _refresh(self) -> None:
        if not self._params:
            self._var.set("（未指定）")
            self._path_var.set("")
            return

        self._var.set(str(self._params.get("name", "")))
        target = Path(str(self._params.get("target", "")))
        note = "" if target.is_file() else "   ⚠ この PC には見つかりません"
        self._path_var.set(f"→ {target}{note}")

    def get(self) -> Any:
        return self._params

    def set(self, value: Any) -> None:
        self._params = dict(value) if value else None
        self._refresh()


class WaitEditor(FieldEditor):
    """完了条件。種類に応じて必要な欄だけを出す。"""

    # 種類ごとに必要な追加項目
    _EXTRA: dict[str, tuple[Field, ...]] = {
        "window": (Field("title", "window_title", "ウィンドウの題名", True),),
        "element": (Field("target", "element", "現れる要素", required=True),),
        "element_enabled": (Field("target", "element", "押せるようになる要素", True),),
        "text_contains": (
            Field("target", "element", "見る場所", required=True),
            Field("value", "text", "現れる文字", required=True),
        ),
        "file_exists": (Field("path", "save_path", "できるファイル", required=True),),
    }

    def _build(self) -> None:
        self._kind_var = tk.StringVar(value=WAIT_KINDS["none"])
        self._editors: dict[str, FieldEditor] = {}
        self._pending: dict[str, Any] = {}

        ttk.Combobox(
            self,
            textvariable=self._kind_var,
            values=list(WAIT_KINDS.values()),
            state="readonly",
            width=26,
        ).grid(row=0, column=0, sticky="w")
        self._kind_var.trace_add("write", lambda *_: self._rebuild())

        self._extra = ttk.Frame(self)
        self._extra.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._extra.columnconfigure(1, weight=1)

    def _kind(self) -> str:
        label = self._kind_var.get()
        for key, text in WAIT_KINDS.items():
            if text == label:
                return key
        return "none"

    def _rebuild(self) -> None:
        for widget in self._extra.winfo_children():
            widget.destroy()
        self._editors.clear()

        for row, field in enumerate(self._EXTRA.get(self._kind(), ())):
            ttk.Label(self._extra, text=field.label).grid(
                row=row, column=0, sticky="w", padx=(16, 8), pady=2
            )
            editor = make_editor(self._extra, field, self.context)
            editor.grid(row=row, column=1, sticky="ew", pady=2)
            self._editors[field.key] = editor

            if field.key in self._pending:
                editor.set(self._pending[field.key])

    def get(self) -> Any:
        kind = self._kind()
        if kind == "none":
            return {"kind": "none"}
        value: dict[str, Any] = {"kind": kind}
        for key, editor in self._editors.items():
            value[key] = editor.get()
        return value

    def set(self, value: Any) -> None:
        data = dict(value or {})
        kind = str(data.get("kind", "none"))
        self._pending = {k: v for k, v in data.items() if k != "kind"}
        self._kind_var.set(WAIT_KINDS.get(kind, WAIT_KINDS["none"]))
        self._rebuild()


_EDITORS: dict[str, type[FieldEditor]] = {
    "text": TextEditor,
    "int": IntEditor,
    "bool": BoolEditor,
    "choice": ChoiceEditor,
    "keys": KeysEditor,
    "date_range": DateRangeEditor,
    "path": PathEditor,
    "save_path": SavePathEditor,
    "folder": FolderEditor,
    "file_pattern": FilePatternEditor,
    "file_name": FileNameEditor,
    "element": ElementEditor,
    "app": AppEditor,
    "wait": WaitEditor,
    "window_title": WindowTitleEditor,
}


def make_editor(
    master: tk.Misc, field: Field, context: EditorContext
) -> FieldEditor:
    """kind に応じたウィジェットを作る。"""
    editor_class = _EDITORS.get(field.kind, TextEditor)
    return editor_class(master, field, context)
