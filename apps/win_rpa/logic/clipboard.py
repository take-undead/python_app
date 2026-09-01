"""クリップボードの読み取りと空にする操作（Tkinter に依存しない）。

画面に出ている数値を UIA から読めないことがある（独自描画の表示欄、
一覧のセル、値を公開しないコントロール）。そのときは対象アプリに
Ctrl+C を送って、こちらでクリップボードから受け取る。

**読む前に必ず空にする。** 空にしないと、コピーに失敗したときに
前から入っていた文字（前の手順の値や、人が別の作業で入れたもの）を
そのまま「読み取った値」として記録してしまう。記録先は後から検算する
データなので、間違った値が黙って 1 行増えるのが最も困る。

クリップボードは OS で 1 つしかなく、他のプロセスが開いている間は
開けない。少し待って開き直す。
"""

from __future__ import annotations

import time

import win32clipboard
import win32con

# 他のプロセスが開いている間は開けない。開くまでの試行回数と間隔
_OPEN_RETRIES = 20
_OPEN_WAIT_SEC = 0.05


class ClipboardError(Exception):
    """クリップボードを扱えなかった。"""


def _open() -> None:
    """クリップボードを開く。開けなければ少し待って試し直す。"""
    last_error: Exception | None = None
    for _ in range(_OPEN_RETRIES):
        try:
            win32clipboard.OpenClipboard()
            return
        except Exception as exc:  # noqa: BLE001 - pywin32 の例外型は環境で変わる
            last_error = exc
            time.sleep(_OPEN_WAIT_SEC)

    raise ClipboardError(
        "クリップボードを開けませんでした。"
        "他のアプリが使っている可能性があります。"
        + (f"（{last_error}）" if last_error else "")
    )


def read_text() -> str:
    """クリップボードの文字を読む。文字が入っていなければ空文字。"""
    _open()
    try:
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return ""
        data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
    except Exception as exc:  # noqa: BLE001
        raise ClipboardError(f"クリップボードを読めませんでした: {exc}") from exc
    finally:
        win32clipboard.CloseClipboard()

    return str(data or "")


def clear() -> None:
    """クリップボードを空にする。

    コピーが効かなかったことを「空のまま」で判断できるようにするため、
    読み取りの直前に呼ぶ。
    """
    _open()
    try:
        win32clipboard.EmptyClipboard()
    except Exception as exc:  # noqa: BLE001
        raise ClipboardError(f"クリップボードを空にできませんでした: {exc}") from exc
    finally:
        win32clipboard.CloseClipboard()
