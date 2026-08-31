"""ウィンドウを HWND から探す（Tkinter に依存しない）。

pywinauto の UIA バックエンドは、デスクトップ配下を走査するときに
共通ダイアログ（クラス #32770 の「名前を付けて保存」など）を取りこぼす。
ウィンドウ自体は Win32 では確かに存在するので、HWND を列挙して
そこから UIA 要素を作れば確実に捕まえられる。

業務アプリは保存ダイアログやメッセージボックスを多用するため、
この経路が無いと実用にならない。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from pywinauto import Desktop
from pywinauto.application import WindowSpecification

_user32 = ctypes.windll.user32
_dwmapi = ctypes.windll.dwmapi
_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# DwmGetWindowAttribute の DWMWA_CLOAKED。
# UWP アプリは閉じても「隠れたまま存在する」ウィンドウを残すことがあり、
# IsWindowVisible では除けない
_DWMWA_CLOAKED = 14

# 共通ダイアログ（保存・開く・メッセージボックス）のクラス名
DIALOG_CLASS = "#32770"

# デスクトップやタスクバーなど、自動操作の対象になり得ないもの
_SHELL_CLASSES = frozenset({"Progman", "Shell_TrayWnd", "WorkerW", "Button"})


def _window_text(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _is_cloaked(hwnd: int) -> bool:
    """画面に出ていないのに存在しているウィンドウかを判定する。"""
    cloaked = wintypes.DWORD()
    result = _dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        _DWMWA_CLOAKED,
        ctypes.byref(cloaked),
        ctypes.sizeof(cloaked),
    )
    return result == 0 and cloaked.value != 0


def _process_id(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def find_windows(
    *,
    title: str | None = None,
    class_name: str | None = None,
    pid: int | None = None,
    visible_only: bool = True,
) -> list[int]:
    """条件に合う最上位ウィンドウの HWND を返す。"""
    found: list[int] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if visible_only and not _user32.IsWindowVisible(hwnd):
            return True
        if pid is not None and _process_id(hwnd) != pid:
            return True
        if class_name is not None and _class_name(hwnd) != class_name:
            return True
        if title is not None and _window_text(hwnd) != title:
            return True
        found.append(hwnd)
        return True

    _user32.EnumWindows(_EnumWindowsProc(callback), 0)
    return found


def list_window_titles(exclude_pid: int | None = None) -> list[str]:
    """今開いているウィンドウの題名を返す。

    「終わった合図」でウィンドウの題名を選ばせるために使う。
    人に打たせると打ち間違いで永久に見つからない手順ができるため。

    デスクトップやタスクバーなど、手順の対象になり得ないものは除く。
    """
    titles: list[str] = []
    seen: set[str] = set()

    for handle in find_windows():
        if exclude_pid is not None and _process_id(handle) == exclude_pid:
            continue
        if _class_name(handle) in _SHELL_CLASSES or _is_cloaked(handle):
            continue

        title = _window_text(handle)
        if not title or title in seen:
            continue
        seen.add(title)
        titles.append(title)

    return sorted(titles)


def foreground_window() -> int | None:
    """いま手前にあるウィンドウの HWND。無ければ None。"""
    hwnd = _user32.GetForegroundWindow()
    return int(hwnd) if hwnd else None


def process_of(hwnd: int) -> int:
    """ウィンドウを持っているプロセスの ID。"""
    return _process_id(hwnd)


def spec(hwnd: int) -> WindowSpecification:
    """HWND から pywinauto のウィンドウ指定を作る。

    find_elements は handle を渡すと走査せずに要素を直接作るので、
    UIA の取りこぼしを回避しつつ child_window() がそのまま使える。
    """
    return Desktop(backend="uia").window(handle=hwnd)


def spec_win32(hwnd: int) -> WindowSpecification:
    """HWND から win32 バックエンドのウィンドウ指定を作る。

    共通ダイアログのファイル名欄は、UIA の SetValue だと COM エラー
    （タイムアウト / 取り消し）で入力できないことがある。win32 の
    WM_SETTEXT 経由なら確実に入る。ダイアログの中身を触るときだけ使う。
    """
    return Desktop(backend="win32").window(handle=hwnd)


def find_window(
    title: str, pid: int | None = None, *, dialog_only: bool = False
) -> WindowSpecification | None:
    """題名の一致する最上位ウィンドウを探す。

    まず指定プロセス内、次に全体で探す。ダイアログを出すのが
    起動したプロセスとは限らないため（ランチャー型のアプリ）。
    """
    class_name = DIALOG_CLASS if dialog_only else None

    for scope in ([pid] if pid is not None else []) + [None]:
        for handle in find_windows(title=title, class_name=class_name, pid=scope):
            try:
                return spec(handle)
            except OSError:
                continue
    return None


def find_dialog(title: str, pid: int | None = None) -> WindowSpecification | None:
    """共通ダイアログ（クラス #32770）を題名で探す。"""
    return find_window(title, pid, dialog_only=True)


def find_any_dialog(
    pid: int | None = None, exclude_pid: int | None = None
) -> WindowSpecification | None:
    """題名を問わず、いま出ている共通ダイアログを 1 つ返す。

    「よろしいですか？」の類は題名がアプリごとにまちまちで、
    人に打たせると 1 文字違いで永久に見つからない手順ができる。
    題名を指定しない場合の受け皿として使う。
    """
    for scope in ([pid] if pid is not None else []) + [None]:
        for handle in find_windows(class_name=DIALOG_CLASS, pid=scope):
            if exclude_pid is not None and _process_id(handle) == exclude_pid:
                continue
            try:
                return spec(handle)
            except OSError:
                continue
    return None
