"""手順の定義（Tkinter に依存しない）。

アクションごとに専用の編集フォームを書くと、種類が増えるたびに UI が
膨らむ。ここで「どんな入力項目を持つか」をデータとして宣言しておき、
画面側は kind を見てウィジェットを選ぶだけにする。

同じ宣言を、保存前の検証と JSON 読み込み時の検証にも使い回す。
"""

from __future__ import annotations

import uuid
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
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
        text         1 行のテキスト（差し込み変数が使える）
        int          数値
        bool         チェックボックス
        choice       ドロップダウン
        keys         キーボードのキー（一覧から選ぶ）
        date_range   更新日の範囲（先月・今月などの型から選ぶ）
        path         既存のファイル・フォルダを選ぶ
        save_path    保存先を指定する
        folder       フォルダ（参照で選ぶか、年月日などの型から組み立てる）
        file_pattern まとめて扱うファイルの選び方（すべて／CSV すべて…）
        file_name    できあがるファイルの名前（型から選ぶ）
        element      ピッカーで採取した要素
        wait         完了条件（専用の小さな編集欄）
        app          ショートカット一覧から選んだアプリ
    """

    key: str
    kind: str
    label: str
    required: bool = False
    default: Any = None
    choices: tuple[str, ...] = ()
    help: str = ""


# ［＋ 手順を追加］の分類。ここに並べた順で入れ子メニューになる。
# 増えた操作を平らに並べると探せなくなるので、宣言側で仕分けておく
CAT_APP = "アプリを操作する"
CAT_FILE = "ファイルとフォルダ"
CAT_CHECK = "確認する"
CAT_OTHER = "その他"

CATEGORIES: tuple[str, ...] = (CAT_APP, CAT_FILE, CAT_CHECK, CAT_OTHER)


@dataclass(frozen=True)
class ActionSpec:
    """アクション 1 種類の定義。"""

    name: str
    label: str
    fields: tuple[Field, ...]
    help: str = ""
    category: str = CAT_OTHER
    # 先に置いておかないと成り立たないアクション名。
    # 満たしていないと［＋ 手順を追加］で選べず、保存前の検証でも弾く
    requires: str = ""


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

# 押し方。左クリックだけでは足りない場面（一覧の項目を開く、右クリック
# メニューを出す）が業務アプリでは普通にあるため、選べるようにしておく
CLICK_MANNERS: tuple[str, ...] = ("左クリック", "ダブルクリック", "右クリック")

# ダイアログのボタン。日本語 Windows でもボタン名が英語のままのものが
# あるので、ここでは押したい意味だけを選ばせ、実際の照合は runner が行う
DIALOG_BUTTONS: tuple[str, ...] = (
    "OK", "はい", "いいえ", "キャンセル", "保存", "開く", "閉じる",
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
        category=CAT_APP,
    ),
    "click": ActionSpec(
        "click",
        "ボタンを押す",
        (
            Field("target", "element", "対象", required=True),
            Field("manner", "choice", "押し方", choices=CLICK_MANNERS,
                  default=CLICK_MANNERS[0]),
            _WAIT_FIELD,
            _TIMEOUT_FIELD,
        ),
        category=CAT_APP,
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
        category=CAT_APP,
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
        category=CAT_APP,
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
        category=CAT_APP,
    ),
    "send_keys": ActionSpec(
        "send_keys",
        "キーを押す",
        (
            Field("target", "element", "対象",
                  help="指定しなければ、いま入力を受けている場所に送る"),
            Field("keys", "keys", "押すキー", required=True, default="{ENTER}"),
            Field("repeat", "int", "回数", default=1),
            _WAIT_FIELD,
            _TIMEOUT_FIELD,
        ),
        help="ボタンが無い操作（Enter で確定、Tab で次の欄、Ctrl+S で保存）を行う",
        category=CAT_APP,
    ),
    "menu_select": ActionSpec(
        "menu_select",
        "メニューから選ぶ",
        (
            Field("path", "text", "たどる順", required=True,
                  help="「ファイル > エクスポート」のように > で区切る"),
            _WAIT_FIELD,
            _TIMEOUT_FIELD,
        ),
        help="メニューバーを順にたどって開く。開いてからでないと選べない項目を"
             "1 手順で扱える",
        category=CAT_APP,
    ),
    "focus_window": ActionSpec(
        "focus_window",
        "ウィンドウを前面に出す",
        (
            Field("title", "window_title", "ウィンドウの題名", required=True),
            Field("timeout", "int", "待ち時間（秒）", default=30),
        ),
        help="操作する画面を手前に出して、以降の手順の対象をそのウィンドウに移す",
        category=CAT_APP,
    ),
    "dialog_button": ActionSpec(
        "dialog_button",
        "メッセージに応じる",
        (
            Field("button", "choice", "押すボタン", required=True,
                  choices=DIALOG_BUTTONS, default="OK"),
            Field("dialog_title", "window_title", "ダイアログの題名",
                  help="空なら、出ているメッセージをそのまま対象にする"),
            Field("optional", "bool", "出ていなければ飛ばす", default=True),
            Field("timeout", "int", "待ち時間（秒）", default=10),
        ),
        help="「よろしいですか？」のような確認に答える。無人実行では"
             "これが無いと止まったまま朝を迎える",
        category=CAT_APP,
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
        category=CAT_FILE,
    ),
    "open_dialog": ActionSpec(
        "open_dialog",
        "「ファイルを開く」に応じる",
        (
            Field("path", "path", "開くファイル", required=True),
            Field(
                "dialog_title", "window_title", "ダイアログの題名", default="開く",
            ),
            Field("timeout", "int", "待ち時間（秒）", default=30),
        ),
        help="対象アプリが出す「開く」ダイアログにファイル名を入れて開く",
        category=CAT_FILE,
    ),
    "assert_text": ActionSpec(
        "assert_text",
        "画面の表示を確認する",
        (
            Field("target", "element", "対象", required=True),
            Field("contains", "text", "含まれるべき文字", required=True),
            _TIMEOUT_FIELD,
        ),
        category=CAT_CHECK,
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
        category=CAT_CHECK,
    ),
    "screenshot": ActionSpec(
        "screenshot",
        "画面を保存する",
        (
            Field("name", "text", "画像の名前", required=True,
                  default="画面_{yyyymmdd}.png"),
        ),
        help="そのときの画面を保存先フォルダに残す。月に 1 回しか動かさないので、"
             "「本当にこの内容で出力したか」を後から見られるようにしておく",
        category=CAT_CHECK,
    ),
    "make_folder": ActionSpec(
        "make_folder",
        "フォルダを作る",
        (
            Field("name", "folder", "フォルダ名", required=True,
                  help="「型 ▼」から年月日などの自動設定を選べる"),
            Field("parent", "folder", "作る場所"),
            Field("set_as_work", "bool", "作ったフォルダを、以降の保存先にする",
                  default=True),
        ),
        help="無ければ作る。既にあっても失敗させない（毎月動かすため）",
        category=CAT_FILE,
    ),
    "set_work_dir": ActionSpec(
        "set_work_dir",
        "保存先フォルダを選ぶ",
        (
            Field("path", "folder", "保存先フォルダ", required=True),
            Field("create", "bool", "無ければ作る", default=True),
        ),
        help="これ以降の手順で、ファイル名だけの指定はこのフォルダが基準になる",
        category=CAT_FILE,
    ),
    "copy_files": ActionSpec(
        "copy_files",
        "ファイルをコピー／移動する",
        (
            Field("source", "file_pattern", "対象のファイル", required=True),
            Field("from_dir", "folder", "探す場所"),
            Field("dest", "folder", "行き先のフォルダ", required=True),
            Field("modified", "date_range", "更新日で絞る",
                  help="ファイル名に日付が入っていないときに使う。"
                       "ファイルの更新日時で選ぶ"),
            Field("mode", "choice", "やり方", choices=("コピー", "移動"),
                  default="コピー"),
            Field("rename", "text", "名前を付け替える",
                  help="空ならそのまま。例: 売上_{yyyymm}"
                       "（2 件以上あるときは末尾に連番が付く）"),
            Field(
                "on_exists", "choice", "同じ名前のとき",
                choices=("飛ばす", "上書きする", "エラーにする"), default="飛ばす",
                help="行き先に同じ名前のファイルがあったときの扱い",
            ),
            Field("min_count", "int", "最低件数", default=1,
                  help="これより少なければ失敗にする。0 件を黙って次に渡さないため"),
        ),
        category=CAT_FILE,
    ),
    "merge_csv": ActionSpec(
        "merge_csv",
        "CSV をまとめる",
        (
            Field("output", "file_name", "できあがる名前", required=True,
                  default="まとめ_{yyyymmdd}.csv"),
            Field("add_source", "bool", "元ファイル名の列を足す", default=True),
            Field("min_rows", "int", "最低行数", default=2),
        ),
        help="「フォルダを作る」で作ったフォルダの CSV を全部読み、"
             "先頭付近にある日時の列で古い順に並べ替えて 1 つにまとめ、"
             "同じフォルダに置く",
        category=CAT_OTHER,
        requires="make_folder",
    ),
    "close_app": ActionSpec(
        "close_app",
        "アプリを閉じる",
        (Field("timeout", "int", "待ち時間（秒）", default=20),),
        category=CAT_APP,
    ),
    "wait_seconds": ActionSpec(
        "wait_seconds",
        "しばらく待つ",
        (Field("seconds", "int", "待つ秒数", required=True, default=5),),
        help="時間で待つのは最後の手段。合図で待てるなら、各手順の"
             "「終わった合図」を使うほうが速くて確実",
    ),
    "run_program": ActionSpec(
        "run_program",
        "別のプログラムを実行する",
        (
            Field("app", "app", "実行するプログラム", required=True),
            Field("wait_exit", "bool", "終わるまで待つ", default=True),
            Field("timeout", "int", "待ち時間（秒）", default=300),
        ),
        help="バッチファイルや別のツールを呼ぶ。終了コードが 0 以外なら失敗にする",
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


def requirement_error(spec: ActionSpec, earlier: list["Step"]) -> str:
    """先に必要な手順が無ければ、その理由を日本語で返す。無ければ空文字。

    「CSV をまとめる」のように、前の手順が決めたものを使うアクションがある。
    置けてしまうと実行時まで気づけないので、置く前に止める。
    """
    if not spec.requires:
        return ""
    if any(step.action == spec.requires and step.enabled for step in earlier):
        return ""

    needed = ACTIONS[spec.requires].label
    return f"先に「{needed}」の手順が必要です"


def actions_by_category() -> list[tuple[str, list[tuple[str, ActionSpec]]]]:
    """アクションを分類ごとに束ねて返す（［＋ 手順を追加］と種類の一覧用）。

    分類は ActionSpec 側に書いてある。ここで組み立てておくことで、
    アクションを 1 つ増やしても ui/ を触らずに済む。
    """
    grouped: dict[str, list[tuple[str, ActionSpec]]] = {
        category: [] for category in CATEGORIES
    }
    for name, spec in ACTIONS.items():
        category = spec.category if spec.category in grouped else CAT_OTHER
        grouped[category].append((name, spec))

    return [
        (category, grouped[category])
        for category in CATEGORIES
        if grouped[category]
    ]


# フォルダ名の型。ユーザーに {yyyymm} を覚えさせず、選んで組ませるための一覧。
# 左が保存される値、右が画面に出す説明。
FOLDER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("{yyyymm}", "年月（202608）"),
    ("{yyyy}-{mm}", "年月（2026-08）"),
    ("{yyyymmdd}", "年月日（20260828）"),
    ("{yyyy}\\{mm}", "年 の下に 月（2026 ＞ 08）"),
    ("{prev_yyyymm}", "前月（202607）"),
    ("{scenario}_{yyyymm}", "シナリオ名＋年月（売上集計_202608）"),
)

# できあがるファイルの名前の型
FILE_NAME_PATTERNS: tuple[tuple[str, str], ...] = (
    ("まとめ_{yyyymmdd}.csv", "まとめ＋今日（まとめ_20260828.csv）"),
    ("まとめ_{yyyymm}.csv", "まとめ＋年月（まとめ_202608.csv）"),
    ("{scenario}_{yyyymm}.csv", "シナリオ名＋年月（売上集計_202608.csv）"),
    ("{scenario}_{prev_yyyymm}.csv", "シナリオ名＋前月（売上集計_202607.csv）"),
)

# まとめて扱うファイルの選び方
FILE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("*", "すべてのファイル"),
    ("*.csv", "CSV すべて"),
    ("*.xlsx", "Excel すべて"),
    ("*.txt", "テキストすべて"),
    ("*{yyyymm}*.csv", "今月分の CSV（名前に 202608 を含む）"),
    ("*{prev_yyyymm}*.csv", "前月分の CSV（名前に 202607 を含む）"),
)


# 押せるキー。左が保存される値（pywinauto の書き方）、右が画面に出す説明。
# {ENTER} や ^s という書き方をユーザーに覚えさせないための一覧
KEY_CHOICES: tuple[tuple[str, str], ...] = (
    ("{ENTER}", "Enter（決定）"),
    ("{TAB}", "Tab（次の項目へ）"),
    ("{ESC}", "Esc（取り消し）"),
    ("{SPACE}", "スペース"),
    ("{BACKSPACE}", "BackSpace（1 文字消す）"),
    ("{DELETE}", "Delete（1 文字消す）"),
    ("{UP}", "↑"),
    ("{DOWN}", "↓"),
    ("{LEFT}", "←"),
    ("{RIGHT}", "→"),
    ("{HOME}", "Home（先頭へ）"),
    ("{END}", "End（末尾へ）"),
    ("{PGUP}", "PageUp（前の画面）"),
    ("{PGDN}", "PageDown（次の画面）"),
    ("^a", "Ctrl+A（すべて選択）"),
    ("^c", "Ctrl+C（コピー）"),
    ("^v", "Ctrl+V（貼り付け）"),
    ("^x", "Ctrl+X（切り取り）"),
    ("^z", "Ctrl+Z（元に戻す）"),
    ("^n", "Ctrl+N（新規）"),
    ("^o", "Ctrl+O（開く）"),
    ("^s", "Ctrl+S（保存）"),
    ("^p", "Ctrl+P（印刷）"),
    ("^f", "Ctrl+F（検索）"),
    ("{F1}", "F1"),
    ("{F2}", "F2"),
    ("{F3}", "F3"),
    ("{F4}", "F4"),
    ("{F5}", "F5（更新）"),
    ("{F6}", "F6"),
    ("{F7}", "F7"),
    ("{F8}", "F8"),
    ("{F9}", "F9"),
    ("{F10}", "F10"),
    ("{F11}", "F11"),
    ("{F12}", "F12"),
    ("%{F4}", "Alt+F4（閉じる）"),
)

KEY_LABELS: dict[str, str] = {value: label for value, label in KEY_CHOICES}


def describe_keys(value: str) -> str:
    """キーの値を、画面と実行ログに出す言い方にする。"""
    return KEY_LABELS.get(value, value)


# メニューをたどる順の区切り。どれで書いても同じに扱う
_MENU_SEPARATORS: tuple[str, ...] = ("->", "→", "＞", ">", "／", "/")


def menu_parts(path: str) -> list[str]:
    """「ファイル > エクスポート」を ["ファイル", "エクスポート"] にする。

    区切りの書き方は人によって違うので、受け取る側で吸収する。
    """
    text = path
    for separator in _MENU_SEPARATORS:
        text = text.replace(separator, "\n")
    return [part.strip() for part in text.split("\n") if part.strip()]


# 更新日の範囲。日付を打たせず、月次で使う言い方から選ばせる。
# 左が保存される値、右が画面に出す説明
DATE_RANGES: tuple[tuple[str, str], ...] = (
    ("none", "指定なし（すべて）"),
    ("prev_month", "先月（1 日〜末日）"),
    ("this_month", "今月（1 日〜今日）"),
    ("today", "今日"),
    ("yesterday", "昨日"),
    ("last_7", "直近 7 日"),
    ("last_30", "直近 30 日"),
    ("custom", "期間を指定する"),
)

DATE_RANGE_LABELS: dict[str, str] = {value: label for value, label in DATE_RANGES}


def _month_start(when: date) -> date:
    return when.replace(day=1)


def _prev_month_start(when: date) -> date:
    return (
        when.replace(year=when.year - 1, month=12, day=1)
        if when.month == 1
        else when.replace(month=when.month - 1, day=1)
    )


def clamp_day(year: int, month: int, day: int) -> date:
    """年・月・日から日付を作る。その月に無い日は末日に丸める。

    日を選ぶ欄は 1〜31 を出すので、2 月 31 日のような組み合わせが来る。
    エラーにせず末日として扱う（「月末まで」を選びたい人の意図に合う）。
    """
    year = max(1, min(9999, year))
    month = max(1, min(12, month))
    last = monthrange(year, month)[1]
    return date(year, month, max(1, min(last, day)))


def resolve_date_range(
    value: Any, today: date | None = None
) -> tuple[datetime | None, datetime | None]:
    """更新日の範囲を、実際の日時（開始・終了）に直す。

    どちらも含む。終了はその日の 23:59:59.999999 まで。
    指定なしのときは (None, None) を返す。
    """
    data = dict(value or {})
    kind = str(data.get("kind", "none"))
    today = today or date.today()

    if kind in ("", "none"):
        return None, None

    if kind == "custom":
        start_day = _parse_day(data.get("from")) or date.min
        end_day = _parse_day(data.get("to")) or date.max
    elif kind == "today":
        start_day = end_day = today
    elif kind == "yesterday":
        start_day = end_day = today - timedelta(days=1)
    elif kind == "this_month":
        start_day, end_day = _month_start(today), today
    elif kind == "prev_month":
        start_day = _prev_month_start(today)
        end_day = _month_start(today) - timedelta(days=1)
    elif kind == "last_7":
        start_day, end_day = today - timedelta(days=6), today
    elif kind == "last_30":
        start_day, end_day = today - timedelta(days=29), today
    else:
        return None, None

    if start_day > end_day:
        start_day, end_day = end_day, start_day

    return (
        datetime.combine(start_day, time.min),
        datetime.combine(end_day, time.max),
    )


def _parse_day(raw: Any) -> date | None:
    """保存されている "2026-08-01" を日付に直す。読めなければ None。"""
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def describe_date_range(value: Any, today: date | None = None) -> str:
    """更新日の範囲を、手順一覧と実行ログに出す言い方にする。"""
    data = dict(value or {})
    kind = str(data.get("kind", "none"))
    if kind in ("", "none"):
        return ""

    if kind != "custom":
        return DATE_RANGE_LABELS.get(kind, kind)

    # 片側だけの指定も、そのまま「いつから」「いつまで」として読ませる。
    # 手で JSON を直したときに片方が欠けることがあり、そこで
    # 0001-01-01 のような日付を見せても何も伝わらない
    start = _parse_day(data.get("from"))
    end = _parse_day(data.get("to"))
    if start and end:
        if start > end:
            start, end = end, start
        return f"{start:%Y-%m-%d} 〜 {end:%Y-%m-%d}"
    if start:
        return f"{start:%Y-%m-%d} から"
    if end:
        return f"{end:%Y-%m-%d} まで"
    return "期間を指定する"


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
def build_variables(
    work_dir: Path, today: date | None = None, scenario_name: str = ""
) -> dict[str, str]:
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
        "prev_yyyy": f"{prev_year:04d}",
        "prev_mm": f"{prev_month:02d}",
        "prev_yyyymm": f"{prev_year:04d}{prev_month:02d}",
        "prev_yyyy-mm": f"{prev_year:04d}-{prev_month:02d}",
        "work_dir": str(work_dir),
        "scenario": scenario_name,
    }


VARIABLE_LABELS: dict[str, str] = {
    "yyyymm": "今年月（202608）",
    "yyyy-mm": "今年月（2026-08）",
    "prev_yyyymm": "前年月（202607）",
    "prev_yyyy-mm": "前年月（2026-07）",
    "yyyy": "年（2026）",
    "mm": "月（08）",
    "dd": "日（28）",
    "yyyymmdd": "今日（20260828）",
    "scenario": "シナリオ名",
    "work_dir": "保存先フォルダ",
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
    """手順 1 つ。

    group は見出しに過ぎない。手順の並びは今まで通り 1 本の直線で、
    実行順は steps の順そのもの。入れ子にすると分岐・繰り返しの UI が
    要るようになるため、束ねるのは「一覧を読むため」だけに留める。
    """

    action: str
    params: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    enabled: bool = True
    group: str = ""

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
                manner = str(self.params.get("manner", CLICK_MANNERS[0]))
                if manner == "ダブルクリック":
                    return f"{what} をダブルクリックする"
                if manner == "右クリック":
                    return f"{what} を右クリックする"
                return f"{what} を押す"
            if self.action == "check":
                on = self.params.get("value", True)
                return f"{what} を{'入' if on else '解除'}にする"
            if self.action == "select":
                return f"{what} で「{self.params.get('value', '')}」を選ぶ"
            if self.action == "set_text":
                return f"{what} に「{self.params.get('value', '')}」を入力する"
            return f"{what} に「{self.params.get('contains', '')}」が出るか確認する"
        if self.action == "send_keys":
            keys = describe_keys(str(self.params.get("keys", "")))
            count = int(self.params.get("repeat", 1) or 1)
            ref = self.element()
            where = f"{describe_element(ref)} で " if ref else ""
            times = f" を {count} 回" if count > 1 else " を"
            return f"{where}{keys}{times}押す"
        if self.action == "menu_select":
            parts = menu_parts(str(self.params.get("path", "")))
            return f"メニュー「{' > '.join(parts)}」を選ぶ" if parts else "メニューを選ぶ"
        if self.action == "focus_window":
            return f"「{self.params.get('title', '')}」を前面に出す"
        if self.action == "dialog_button":
            title = str(self.params.get("dialog_title", "")).strip()
            where = f"「{title}」で" if title else "出ているメッセージで"
            return f"{where}「{self.params.get('button', '')}」を押す"
        if self.action == "save_dialog":
            return f"保存ダイアログに「{Path(self.params.get('path', '')).name}」で保存する"
        if self.action == "open_dialog":
            return f"開くダイアログで「{Path(self.params.get('path', '')).name}」を開く"
        if self.action == "screenshot":
            return f"画面を「{self.params.get('name', '')}」に残す"
        if self.action == "wait_seconds":
            return f"{self.params.get('seconds', 0)} 秒待つ"
        if self.action == "run_program":
            app = self.params.get("app") or {}
            return f"{app.get('name', '(未指定)')} を実行する"
        if self.action == "assert_file":
            return f"「{Path(self.params.get('path', '')).name}」ができたか確認する"
        if self.action == "merge_csv":
            return (
                f"フォルダの CSV を日時順にまとめて"
                f"「{self.params.get('output', '')}」にする"
            )
        if self.action == "make_folder":
            return f"フォルダ「{self.params.get('name', '')}」を作る"
        if self.action == "set_work_dir":
            return f"保存先を「{self.params.get('path', '')}」にする"
        if self.action == "copy_files":
            verb = "移動" if self.params.get("mode") == "移動" else "コピー"
            when = describe_date_range(self.params.get("modified"))
            narrowed = f"（更新日: {when}）" if when else ""
            return (
                f"{self.params.get('source', '')}{narrowed} を"
                f"「{self.params.get('dest', '')}」へ{verb}する"
            )
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
        data: dict[str, Any] = {
            "id": self.id,
            "action": self.action,
            "enabled": self.enabled,
            "params": self.params,
        }
        # 束ねていないときは書かない。既存のシナリオの見た目を変えないため
        if self.group:
            data["group"] = self.group
        return data

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
            group=str(data.get("group", "")),
        )


@dataclass
class Scenario:
    """手順一式。"""

    name: str
    work_dir: str = ""
    steps: list[Step] = field(default_factory=list)

    def resolved_work_dir(self, base: Path) -> Path:
        """作業フォルダを決める。未指定ならシナリオ名のフォルダを使う。

        ここで決まるのは出発点で、実行中に「保存先フォルダを選ぶ」
        「フォルダを作る」の手順があれば、そこから先は切り替わる。
        """
        if self.work_dir:
            return Path(self.work_dir)
        return base / self.name

    def groups(self) -> list[tuple[str, list[int]]]:
        """手順を、隣り合う同じグループごとに束ねて返す。

        入れ子の入れ物を持たず、Step.group が同じものが続いている区間を
        1 つの塊として扱う。並びが正であることを保存形式で保証しなくて
        よくなり、読み込んだ古いシナリオもそのまま扱える。
        """
        blocks: list[tuple[str, list[int]]] = []
        for index, step in enumerate(self.steps):
            if blocks and blocks[-1][0] == step.group:
                blocks[-1][1].append(index)
            else:
                blocks.append((step.group, [index]))
        return blocks

    def group_names(self) -> list[str]:
        """今あるグループ名を、出てくる順に返す（重複なし）。"""
        names: list[str] = []
        for step in self.steps:
            if step.group and step.group not in names:
                names.append(step.group)
        return names

    def normalize_groups(self) -> None:
        """同じ名前のグループが離れて置かれていたら、先に出たほうへ寄せる。

        グループから 1 つ外に出したときなどに、同名の塊が 2 つに割れて
        一覧に同じ見出しが並ぶのを防ぐ。並べ替えるのはグループ操作の
        直後だけで、↑↓ の入れ替えでは呼ばない。
        """
        ordered: list[Step] = []
        placed: set[int] = set()

        for index, step in enumerate(self.steps):
            if index in placed:
                continue
            if not step.group:
                ordered.append(step)
                placed.add(index)
                continue
            for other in range(index, len(self.steps)):
                if other not in placed and self.steps[other].group == step.group:
                    ordered.append(self.steps[other])
                    placed.add(other)

        self.steps = ordered

    def step_problems(self, index: int) -> list[str]:
        """手順 1 つの問題を、前に必要な手順の有無まで含めて返す。

        Step 自身は自分が何番目かを知らないので、前提条件の判定はここで行う。
        """
        step = self.steps[index]
        problems = step.validate()

        problem = requirement_error(step.spec, self.steps[:index])
        if problem:
            problems.append(problem)
        return problems

    def validate(self) -> list[str]:
        """保存・実行の前に、手順全体を検証する。"""
        problems: list[str] = []
        if not self.name.strip():
            problems.append("シナリオ名が空です")
        if not self.steps:
            problems.append("手順が 1 つもありません")

        for index, step in enumerate(self.steps):
            for problem in self.step_problems(index):
                problems.append(f"手順 {index + 1}（{step.spec.label}）: {problem}")

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
