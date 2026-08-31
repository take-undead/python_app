"""カーソル位置の UI 要素を採取する（Tkinter に依存しない）。

自動操作の手順を組むとき、対象アプリのボタン名や AutomationId を人が
書き写すのは現実的でない。目的のウィジェットにマウスを乗せてホットキーを
押すだけで、照合に必要な識別子一式を取れるようにする。

記録するのは座標ではなく「要素の同一性」。座標を記録するとウィンドウが
少しでも動いた時点で壊れるため。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto.uia_defines import IUIA
from pywinauto.uia_element_info import UIAElementInfo

# デスクトップ（要素ツリーの頂点）のクラス名。親をたどる終点の判定に使う
_DESKTOP_CLASS = "#32769"

# 仮想キーコード
VK_F8 = 0x77
VK_ESCAPE = 0x1B

_user32 = ctypes.windll.user32


class PickerError(Exception):
    """要素の採取に失敗した。"""


@dataclass(frozen=True)
class ElementRef:
    """照合に使う要素の識別情報。

    照合は auto_id → name → help_text → legacy_name → index_path の順に試す。
    class_name は記録するが照合には使わない（WinForms のクラス名は
    実行ごとに変わるハッシュを含むため）。

    help_text と legacy_name は、**アイコンだけのツールバーボタン**のために
    ある。UIA の Name が空でも、ツールチップや古い形式（MSAA）の名前は
    入っていることが多い。ここが取れれば、位置で探さずに済む。
    """

    auto_id: str
    name: str
    control_type: str
    class_name: str
    framework: str
    window_title: str
    window_auto_id: str
    # ウィンドウ直下から対象までの (control_type, 同種の中での位置) の並び
    index_path: tuple[tuple[str, int], ...] = field(default=())
    # ツールチップ（UIA の HelpText）
    help_text: str = ""
    # 古い形式（LegacyIAccessible）の名前
    legacy_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["index_path"] = [list(step) for step in self.index_path]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ElementRef":
        # 知らないキーは捨てる。項目を増やす前に保存されたシナリオと、
        # 逆に新しい項目を持つシナリオを古いコードで開いた場合の両方で落ちない
        known = {f.name for f in fields(cls)}
        raw = {key: value for key, value in data.items() if key in known}
        raw["index_path"] = tuple(
            (str(step[0]), int(step[1])) for step in raw.get("index_path", ())
        )
        return cls(**raw)


# ----------------------------------------------------------------------
# 採取
# ----------------------------------------------------------------------
def cursor_pos() -> tuple[int, int]:
    """マウスカーソルの画面座標を返す。"""
    point = wintypes.POINT()
    if not _user32.GetCursorPos(ctypes.byref(point)):
        raise PickerError("マウスカーソルの位置を取得できませんでした。")
    return point.x, point.y


def is_key_pressed(vk: int) -> bool:
    """キーが今押されているかを返す（定期処理から呼ぶ）。"""
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def capture_at(x: int, y: int) -> ElementRef:
    """指定した画面座標にある要素を採取する。"""
    try:
        element = IUIA().iuia.ElementFromPoint(wintypes.POINT(x, y))
    except OSError as exc:
        raise PickerError(
            f"座標 ({x}, {y}) の要素を取得できませんでした。"
        ) from exc
    if not element:
        raise PickerError(f"座標 ({x}, {y}) には UI 要素がありません。")

    info = UIAElementInfo(element)
    chain = _chain_to_window(info)
    window = chain[0]

    return ElementRef(
        auto_id=_safe(info.automation_id),
        name=_safe(info.name),
        control_type=_safe(info.control_type),
        class_name=_safe(info.class_name),
        framework=_safe(info.framework_id),
        window_title=_safe(window.name),
        window_auto_id=_safe(window.automation_id),
        index_path=_index_path(chain),
        help_text=help_text_of(info),
        legacy_name=legacy_name_of(info),
    )


def help_text_of(info: UIAElementInfo) -> str:
    """ツールチップ（UIA の HelpText）を読む。

    アイコンだけのボタンは Name が空でもここに説明が入っていることが多い。
    """
    try:
        return _safe(info.element.CurrentHelpText)
    except Exception:  # noqa: BLE001 - COM は種類が定まらない
        return ""


def legacy_name_of(info: UIAElementInfo) -> str:
    """古い形式（LegacyIAccessible / MSAA）の名前を読む。

    Win32 や MFC のツールバーは、UIA の Name を出さずにこちらだけ持つ
    ことがある。
    """
    try:
        return _safe(UIAWrapper(info).legacy_properties().get("Name"))
    except Exception:  # noqa: BLE001 - パターン未対応の要素では失敗する
        return ""


def capture_at_cursor() -> ElementRef:
    """今カーソルがある位置の要素を採取する。"""
    return capture_at(*cursor_pos())


def peek_at_cursor() -> str:
    """カーソル下に何があるかを短い文で返す。

    採取前の下見用。構造上の位置は求めない（ツリーをたどるので重く、
    定期的に呼ぶ用途に向かないため）。
    """
    try:
        x, y = cursor_pos()
        element = IUIA().iuia.ElementFromPoint(wintypes.POINT(x, y))
        if not element:
            return "（UI 要素なし）"
        info = UIAElementInfo(element)
    except (OSError, PickerError):
        return "（読み取れません）"

    label = _TYPE_LABEL.get(_safe(info.control_type), _safe(info.control_type))
    name = _safe(info.name)
    if name:
        return f"{label}「{name}」"
    auto_id = _safe(info.automation_id)
    if auto_id:
        return f"{label}（{auto_id}）"
    # 名前が無くてもツールチップがあれば、狙いが合っているかは分かる
    hint = help_text_of(info) or legacy_name_of(info)
    if hint:
        return f"{label}〔{hint}〕"
    return f"名前のない{label}"


# ----------------------------------------------------------------------
# 採取結果の解釈
# ----------------------------------------------------------------------
# 要素の種類から操作を推定する。ユーザーに選ばせる場面を減らすため
_ACTION_BY_TYPE: dict[str, str] = {
    "Button": "click",
    "SplitButton": "click",
    "Hyperlink": "click",
    "MenuItem": "click",
    "TabItem": "click",
    "TreeItem": "click",
    "ListItem": "click",
    "CheckBox": "check",
    "RadioButton": "check",
    "ComboBox": "select",
    "List": "select",
    "Edit": "set_text",
    "Document": "set_text",
    "Text": "assert_text",
}

# 画面に出す日本語名
_TYPE_LABEL: dict[str, str] = {
    "Button": "ボタン",
    "SplitButton": "ボタン",
    "CheckBox": "チェックボックス",
    "RadioButton": "ラジオボタン",
    "ComboBox": "選択欄",
    "List": "一覧",
    "ListItem": "一覧の項目",
    "Edit": "入力欄",
    "Document": "入力欄",
    "Text": "表示欄",
    "MenuItem": "メニュー",
    "TabItem": "タブ",
    "TreeItem": "ツリーの項目",
    "Hyperlink": "リンク",
    "Window": "ウィンドウ",
    "Pane": "領域",
}


def suggest_action(ref: ElementRef) -> str:
    """要素の種類から、既定の操作を推定する。"""
    return _ACTION_BY_TYPE.get(ref.control_type, "click")


def describe(ref: ElementRef) -> str:
    """一覧に表示する日本語の説明を作る。"""
    label = _TYPE_LABEL.get(ref.control_type, ref.control_type or "要素")
    if ref.name:
        return f"{label}「{ref.name}」"
    if ref.auto_id:
        return f"{label}（{ref.auto_id}）"
    hint = ref.help_text or ref.legacy_name
    if hint:
        return f"{label}〔{hint}〕"
    if ref.index_path:
        # 名前のないボタンが並ぶと一覧で見分けが付かないので位置を出す
        return f"名前のない{label}（{ref.index_path[-1][1] + 1} 個目）"
    return f"名前のない{label}"


def is_identifiable(ref: ElementRef) -> bool:
    """名前で確実に見つけられる要素かを判定する。

    AutomationId・名前のほか、ツールチップと古い形式の名前も見る。
    アイコンだけのツールバーボタンは、これらだけを持っていることがある。
    """
    return bool(ref.auto_id or ref.name or ref.help_text or ref.legacy_name)


def is_positional_only(ref: ElementRef) -> bool:
    """名前の類が一切なく、構造上の位置でしか探せない要素かを判定する。

    探せないわけではない（実行側の 3 段目が使える）が、対象アプリの更新で
    ボタンが増減すると隣を押すことになる。使う前に人に確認する。
    """
    return not is_identifiable(ref) and bool(ref.index_path)


# ----------------------------------------------------------------------
# 内部
# ----------------------------------------------------------------------
def _safe(value: Any) -> str:
    """UIA が None や COM エラーを返すことがあるので文字列に正規化する。"""
    try:
        return str(value) if value else ""
    except OSError:
        return ""


def _chain_to_window(info: UIAElementInfo) -> list[UIAElementInfo]:
    """トップレベルウィンドウから対象要素までの並びを返す。"""
    chain: list[UIAElementInfo] = [info]
    current = info

    # 万一ツリーが循環していても止まるよう上限を設ける
    for _ in range(64):
        try:
            parent = current.parent
        except OSError:
            break
        if parent is None or _safe(parent.class_name) == _DESKTOP_CLASS:
            break
        chain.append(parent)
        current = parent

    chain.reverse()
    return chain


def _index_path(chain: list[UIAElementInfo]) -> tuple[tuple[str, int], ...]:
    """auto_id も name も変わったときに使う、構造上の位置を求める。

    兄弟全体での通し番号ではなく「同じ種類の中で何番目か」を記録する。
    アプリ更新で装飾用の要素が増えてもずれにくいため。
    """
    path: list[tuple[str, int]] = []

    for depth in range(1, len(chain)):
        parent, child = chain[depth - 1], chain[depth]
        control_type = _safe(child.control_type)
        try:
            siblings = parent.children()
        except OSError:
            path.append((control_type, 0))
            continue

        index = 0
        for sibling in siblings:
            if _safe(sibling.control_type) != control_type:
                continue
            if sibling == child:
                break
            index += 1
        path.append((control_type, index))

    return tuple(path)
