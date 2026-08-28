"""手順の定義（Tkinter に依存しない）。

アクションごとに専用の編集フォームを書くと、種類が増えるたびに UI が
膨らむ。ここで「どんな入力項目を持つか」をデータとして宣言しておき、
画面側は kind を見てウィジェットを選ぶだけにする。

同じ宣言を、保存前の検証と JSON 読み込み時の検証にも使い回す。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from logic.picker import ElementRef, describe as describe_element


class ScenarioError(Exception):
    """手順の内容が不正、または読み書きに失敗した。"""


# ----------------------------------------------------------------------
# 入力項目の宣言
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Field:
    """アクションが受け取る入力項目 1 つ。

    kind が画面側のウィジェットを決める:
        text      1 行のテキスト（差し込み変数が使える）
        int       数値
        bool      チェックボックス
        choice    ドロップダウン
        path      既存のファイル・フォルダを選ぶ
        save_path 保存先を指定する
        element   ピッカーで採取した要素
        wait      完了条件（専用の小さな編集欄）
        app       ショートカット一覧から選んだアプリ
    """

    key: str
    kind: str
    label: str
    required: bool = False
    default: Any = None
    choices: tuple[str, ...] = ()
    help: str = ""


@dataclass(frozen=True)
class ActionSpec:
    """アクション 1 種類の定義。"""

    name: str
    label: str
    fields: tuple[Field, ...]
    help: str = ""


# 完了条件は複数のアクションで共通なので使い回す
_WAIT_FIELD = Field(
    "wait_for",
    "wait",
    "終わった合図",
    help="次の手順に進んでよいと判断する条件。指定しないと即座に次へ進む",
)
_TIMEOUT_FIELD = Field(
    "timeout", "int", "待ち時間（秒）", default=15,
    help="この秒数を過ぎても合図が来なければエラーにする",
)


ACTIONS: dict[str, ActionSpec] = {
    "launch_app": ActionSpec(
        "launch_app",
        "アプリを起動する",
        (
            Field("app", "app", "起動するアプリ", required=True),
            _WAIT_FIELD,
            Field("timeout", "int", "待ち時間（秒）", default=60),
        ),
    ),
    "click": ActionSpec(
        "click",
        "ボタンを押す",
        (
            Field("target", "element", "対象", required=True),
            _WAIT_FIELD,
            _TIMEOUT_FIELD,
        ),
    ),
    "set_text": ActionSpec(
        "set_text",
        "文字を入力する",
        (
            Field("target", "element", "対象", required=True),
            Field("value", "text", "入力する値", required=True),
            _WAIT_FIELD,
            _TIMEOUT_FIELD,
        ),
    ),
    "check": ActionSpec(
        "check",
        "チェックを切り替える",
        (
            Field("target", "element", "対象", required=True),
            Field("value", "bool", "チェックを入れる", default=True),
            _WAIT_FIELD,
            _TIMEOUT_FIELD,
        ),
    ),
    "select": ActionSpec(
        "select",
        "一覧から選ぶ",
        (
            Field("target", "element", "対象", required=True),
            Field("value", "text", "選ぶ項目", required=True),
            _WAIT_FIELD,
            _TIMEOUT_FIELD,
        ),
    ),
    "save_dialog": ActionSpec(
        "save_dialog",
        "「名前を付けて保存」に応じる",
        (
            Field("path", "save_path", "保存先", required=True),
            Field(
                "dialog_title",
                "window_title",
                "ダイアログの題名",
                default="名前を付けて保存",
            ),
            Field("timeout", "int", "待ち時間（秒）", default=30),
        ),
        help="対象アプリが出す保存ダイアログにファイル名を入れて保存する",
    ),
    "assert_text": ActionSpec(
        "assert_text",
        "画面の表示を確認する",
        (
            Field("target", "element", "対象", required=True),
            Field("contains", "text", "含まれるべき文字", required=True),
            _TIMEOUT_FIELD,
        ),
    ),
    "assert_file": ActionSpec(
        "assert_file",
        "ファイルができたか確認する",
        (
            Field("path", "save_path", "確認するファイル", required=True),
            Field("min_rows", "int", "最低行数", default=2),
            Field("timeout", "int", "待ち時間（秒）", default=60),
        ),
        help="空ファイルを黙って次に渡さないための歯止め",
    ),
    "merge_csv": ActionSpec(
        "merge_csv",
        "CSV をまとめる",
        (
            Field("pattern", "text", "まとめる対象", required=True,
                  help="例: 売上_*.csv （作業フォルダからの相対）"),
            Field("output", "save_path", "出力先", required=True),
            Field("add_source", "bool", "元ファイル名の列を足す", default=True),
            Field("min_rows", "int", "最低行数", default=2),
        ),
    ),
    "close_app": ActionSpec(
        "close_app",
        "アプリを閉じる",
        (Field("timeout", "int", "待ち時間（秒）", default=20),),
    ),
    "run_python": ActionSpec(
        "run_python",
        "用意した処理を実行する",
        (
            Field("script", "path", "スクリプト", required=True),
            Field("args", "text", "引数"),
            Field("timeout", "int", "待ち時間（秒）", default=300),
        ),
        help="標準の操作で足りないときの逃げ道。手順ごとに用意する",
    ),
}


# 完了条件の種類
WAIT_KINDS: dict[str, str] = {
    "none": "待たない",
    "window": "ウィンドウが出る",
    "element": "要素が現れる",
    "element_enabled": "要素が押せるようになる",
    "text_contains": "表示に文字が現れる",
    "file_exists": "ファイルができる",
}


# ----------------------------------------------------------------------
# 差し込み変数
# ----------------------------------------------------------------------
def build_variables(work_dir: Path, today: date | None = None) -> dict[str, str]:
    """テキスト項目に差し込める値を作る。

    月次で動かすので、年月の展開は必須。前月を使う場面が多い
    （月初に前月分を集計するため）。
    """
    today = today or date.today()
    prev_year, prev_month = (
        (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    )

    return {
        "yyyy": f"{today.year:04d}",
        "mm": f"{today.month:02d}",
        "dd": f"{today.day:02d}",
        "yyyymm": f"{today.year:04d}{today.month:02d}",
        "yyyy-mm": f"{today.year:04d}-{today.month:02d}",
        "yyyymmdd": f"{today.year:04d}{today.month:02d}{today.day:02d}",
        "prev_yyyymm": f"{prev_year:04d}{prev_month:02d}",
        "prev_yyyy-mm": f"{prev_year:04d}-{prev_month:02d}",
        "work_dir": str(work_dir),
    }


VARIABLE_LABELS: dict[str, str] = {
    "yyyymm": "今年月（202608）",
    "yyyy-mm": "今年月（2026-08）",
    "prev_yyyymm": "前年月（202607）",
    "prev_yyyy-mm": "前年月（2026-07）",
    "yyyy": "年（2026）",
    "mm": "月（08）",
    "yyyymmdd": "今日（20260827）",
    "work_dir": "作業フォルダ",
}


def expand(text: str, variables: dict[str, str]) -> str:
    """{yyyymm} のような差し込みを展開する。

    str.format は使わない。対象アプリに渡す文字列に { } が
    そのまま含まれることがあり、意図せず落ちるため。
    """
    result = text
    for key, value in variables.items():
        result = result.replace("{" + key + "}", value)
    return result


# ----------------------------------------------------------------------
# 手順
# ----------------------------------------------------------------------
@dataclass
class Step:
    """手順 1 つ。"""

    action: str
    params: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    enabled: bool = True

    @property
    def spec(self) -> ActionSpec:
        try:
            return ACTIONS[self.action]
        except KeyError as exc:
            raise ScenarioError(f"知らない操作です: {self.action}") from exc

    def element(self, key: str = "target") -> ElementRef | None:
        """要素の項目を ElementRef として取り出す。"""
        raw = self.params.get(key)
        if not raw:
            return None
        return ElementRef.from_dict(raw)

    def describe(self) -> str:
        """手順一覧に出す 1 行を作る。JSON を見せないための表示。"""
        spec = self.spec

        if self.action == "launch_app":
            app = self.params.get("app") or {}
            return f"{app.get('name', '(未指定)')} を起動する"
        if self.action in ("click", "check", "select", "set_text", "assert_text"):
            ref = self.element()
            what = describe_element(ref) if ref else "(対象未指定)"
            if self.action == "click":
                return f"{what} を押す"
            if self.action == "check":
                on = self.params.get("value", True)
                return f"{what} を{'入' if on else '解除'}にする"
            if self.action == "select":
                return f"{what} で「{self.params.get('value', '')}」を選ぶ"
            if self.action == "set_text":
                return f"{what} に「{self.params.get('value', '')}」を入力する"
            return f"{what} に「{self.params.get('contains', '')}」が出るか確認する"
        if self.action == "save_dialog":
            return f"保存ダイアログに「{Path(self.params.get('path', '')).name}」で保存する"
        if self.action == "assert_file":
            return f"「{Path(self.params.get('path', '')).name}」ができたか確認する"
        if self.action == "merge_csv":
            return f"{self.params.get('pattern', '')} をまとめる"
        if self.action == "run_python":
            return f"{Path(self.params.get('script', '')).name} を実行する"
        return spec.label

    def validate(self) -> list[str]:
        """埋まっていない必須項目を日本語で返す。"""
        problems: list[str] = []
        for spec_field in self.spec.fields:
            if not spec_field.required:
                continue
            value = self.params.get(spec_field.key)
            if value in (None, "", {}, []):
                problems.append(f"「{spec_field.label}」が未指定です")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "enabled": self.enabled,
            "params": self.params,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Step":
        action = data.get("action", "")
        if action not in ACTIONS:
            raise ScenarioError(f"知らない操作が含まれています: {action}")
        return cls(
            action=action,
            params=dict(data.get("params", {})),
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass
class Scenario:
    """手順一式。"""

    name: str
    work_dir: str = ""
    steps: list[Step] = field(default_factory=list)

    def resolved_work_dir(self, base: Path) -> Path:
        """作業フォルダを決める。未指定ならシナリオ名のフォルダを使う。"""
        if self.work_dir:
            return Path(self.work_dir)
        return base / self.name

    def validate(self) -> list[str]:
        """保存・実行の前に、手順全体を検証する。"""
        problems: list[str] = []
        if not self.name.strip():
            problems.append("シナリオ名が空です")
        if not self.steps:
            problems.append("手順が 1 つもありません")

        for index, step in enumerate(self.steps, start=1):
            for problem in step.validate():
                problems.append(f"手順 {index}（{step.spec.label}）: {problem}")

        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "work_dir": self.work_dir,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scenario":
        return cls(
            name=str(data.get("name", "")),
            work_dir=str(data.get("work_dir", "")),
            steps=[Step.from_dict(item) for item in data.get("steps", [])],
        )
