"""フォルダ選択ダイアログ（Tkinter に依存しない）。

**`tkinter.filedialog.askdirectory` は使えない。**

`pywinauto` は import 時に `sys.coinit_flags = 0`（COINIT_MULTITHREADED）を
設定するため、このアプリのプロセスは MTA になる。一方 `askdirectory` が
呼ぶシェルのフォルダ選択（`SHBrowseForFolder`）は **STA を要求する**ので、
MTA のスレッドから呼ぶと**ダイアログが出ないまま固まる**。

同じ理由で影響を受けるのはフォルダ選択だけ。ファイル選択
（`askopenfilename` / `asksaveasfilename`）は comdlg32 の `GetOpenFileName`
なので MTA でも問題なく開く。

そのためここでは Win32 の `SHBrowseForFolderW` を直に呼ぶ。
**必ず専用スレッドから呼ぶこと**（この関数がそのスレッドを STA で初期化する）。
メインスレッドから呼ぶと FolderDialogError になる。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
from typing import Callable

_ole32 = ctypes.windll.ole32
_shell32 = ctypes.windll.shell32
_user32 = ctypes.windll.user32

COINIT_APARTMENTTHREADED = 0x2
_S_OK = 0
_S_FALSE = 1
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106

# BROWSEINFO の ulFlags
_BIF_RETURNONLYFSDIRS = 0x0001   # ファイルシステム上のフォルダだけ返す
_BIF_NEWDIALOGSTYLE = 0x0040     # 大きさを変えられる今の見た目＋新規作成ボタン
_BIF_EDITBOX = 0x0010            # パスを直接打てる欄

# フォルダ選択に送るメッセージ
_BFFM_INITIALIZED = 1
_BFFM_SETSELECTIONW = 0x0467     # WM_USER + 103
_WM_CLOSE = 0x0010

_BFFCALLBACK = ctypes.WINFUNCTYPE(
    ctypes.c_int, wintypes.HWND, wintypes.UINT, wintypes.LPARAM, wintypes.LPARAM
)


class FolderDialogError(Exception):
    """フォルダ選択ダイアログを開けなかった。"""


class _BrowseInfo(ctypes.Structure):
    _fields_ = [
        ("hwndOwner", wintypes.HWND),
        ("pidlRoot", ctypes.c_void_p),
        ("pszDisplayName", wintypes.LPWSTR),
        ("lpszTitle", wintypes.LPCWSTR),
        ("ulFlags", wintypes.UINT),
        ("lpfn", _BFFCALLBACK),
        ("lParam", wintypes.LPARAM),
        ("iImage", ctypes.c_int),
    ]


# HWND / PIDL は 64bit。argtypes を宣言しないと切り詰められて空振りする
_shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(_BrowseInfo)]
_shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
_shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
_shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
_ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
_ole32.CoTaskMemFree.restype = None
_user32.SendMessageW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.PostMessageW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
]

# ダイアログが開いた瞬間に、その HWND を知らせるための関数
_OnOpened = Callable[[int], None]


def _existing_ancestor(folder: Path | None) -> str:
    """初期選択に使えるフォルダを探す。

    まだ作られていないフォルダ（作業フォルダの初回など）を渡されることが
    あるので、実在する親までさかのぼる。
    """
    if folder is None:
        return ""
    current = folder if folder.is_absolute() else folder.resolve()
    for candidate in (current, *current.parents):
        if candidate.is_dir():
            return str(candidate)
    return ""


def close_dialog(hwnd: int) -> None:
    """開いているフォルダ選択を閉じる（取り消し扱い）。

    画面を閉じるときに呼ぶ。放っておくと、開いたままのダイアログを抱えた
    スレッドが残る。
    """
    if hwnd:
        _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)


def _browse(
    initial: str, title: str, on_opened: _OnOpened | None
) -> Path | None:
    """SHBrowseForFolderW を呼ぶ。STA で初期化済みのスレッドから呼ぶこと。"""
    selected = ctypes.create_unicode_buffer(initial or "")

    def on_message(hwnd, message, _lparam, _data) -> int:
        if message == _BFFM_INITIALIZED:
            if initial:
                _user32.SendMessageW(
                    hwnd, _BFFM_SETSELECTIONW, 1, ctypes.addressof(selected)
                )
            # 親を指定していないので、自分で前面に出す
            _user32.SetForegroundWindow(hwnd)
            if on_opened is not None:
                on_opened(int(hwnd))
        return 0

    # 呼び出しの間、参照を保持しないと GC される
    callback = _BFFCALLBACK(on_message)

    info = _BrowseInfo()
    # 親ウィンドウは渡さない。別スレッドの HWND を親にすると
    # 入力キューが結合され、それ自体が固まる原因になる
    info.hwndOwner = None
    info.pidlRoot = None
    info.pszDisplayName = None
    info.lpszTitle = title or "フォルダを選ぶ"
    info.ulFlags = _BIF_RETURNONLYFSDIRS | _BIF_NEWDIALOGSTYLE | _BIF_EDITBOX
    info.lpfn = callback
    info.lParam = 0
    info.iImage = 0

    pidl = _shell32.SHBrowseForFolderW(ctypes.byref(info))
    if not pidl:
        return None   # 取り消された

    try:
        buffer = ctypes.create_unicode_buffer(1024)
        if not _shell32.SHGetPathFromIDListW(pidl, buffer):
            raise FolderDialogError(
                "選ばれた場所はフォルダとして扱えません。"
                "（PC 内の実在するフォルダを選んでください）"
            )
        return Path(buffer.value)
    finally:
        _ole32.CoTaskMemFree(pidl)


def choose_folder(
    initial: Path | None = None,
    title: str = "",
    on_opened: _OnOpened | None = None,
) -> Path | None:
    """フォルダを選ばせる。取り消されたら None。

    **専用スレッドから呼ぶこと。** このアプリのメインスレッドは pywinauto に
    よって MTA になっているため、そこから呼ぶと開けない。

    on_opened には開いたダイアログの HWND を渡す。画面を閉じるときに
    close_dialog() へ渡して片付けるため。
    """
    result = _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if result == _RPC_E_CHANGED_MODE:
        raise FolderDialogError(
            "フォルダ選択は専用のスレッドから開く必要があります"
            "（このスレッドは既に MTA として初期化されています）。"
        )
    if result not in (_S_OK, _S_FALSE):
        raise FolderDialogError(
            f"COM を初期化できませんでした（0x{result & 0xFFFFFFFF:08X}）。"
        )

    try:
        return _browse(_existing_ancestor(initial), title, on_opened)
    finally:
        _ole32.CoUninitialize()
