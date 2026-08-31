"""デスクトップとスタートメニューのショートカットを集める（Tkinter に依存しない）。

起動するアプリを選ぶとき、exe のフルパスを人に入力させない。
ショートカットには起動引数と作業フォルダも入っているので、
exe を直接指定するより起動が安定する。

**ショートカットかどうかは拡張子ではなく中身で判断する。** 業務アプリが
`.kww` のような固有拡張子でショートカットを配っていることがあり、
拡張子で弾くと一覧にも出せず起動もできない。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pythoncom
import win32com.client
from win32com.shell import shell as shellcon

# シェルリンクの先頭 20 バイト。ヘッダ長 0x4C と CLSID_ShellLink が入っている
_LINK_HEADER_SIZE = 0x4C
_LINK_CLSID = bytes.fromhex("0114020000000000c000000000000046")

# ショートカットを探す場所。(表示名, SpecialFolders のキー) の並び
_SEARCH_FOLDERS: tuple[tuple[str, str], ...] = (
    ("デスクトップ", "Desktop"),
    ("デスクトップ（共通）", "AllUsersDesktop"),
    ("スタートメニュー", "Programs"),
    ("スタートメニュー（共通）", "AllUsersPrograms"),
)

# 一覧に出さないもの（アンインストーラや説明書き）
_EXCLUDE_KEYWORDS: tuple[str, ...] = (
    "uninstall",
    "unistall",  # 実在の綴り間違い（インストーラ側の表記ゆれ）
    "unins",
    "アンインストール",
    "readme",
    "お読みください",
    "help",
    "ヘルプ",
)


class ShortcutError(Exception):
    """ショートカットの解決に失敗した。"""


@dataclass(frozen=True)
class Shortcut:
    """ショートカット 1 件。"""

    name: str
    lnk: Path
    target: Path
    args: str
    work_dir: Path | None
    source: str

    @property
    def exists(self) -> bool:
        """リンク先が今この PC に存在するか。"""
        return self.target.is_file()

    @property
    def command(self) -> str:
        """起動用のコマンドライン。"""
        if self.args:
            return f'"{self.target}" {self.args}'
        return f'"{self.target}"'


def _ensure_com() -> None:
    """ワーカースレッドから呼ばれても動くよう COM を初期化する。

    UI をふさがないよう一覧の収集は別スレッドで行うため。
    """
    try:
        pythoncom.CoInitialize()
    except pythoncom.com_error:
        pass  # 既に初期化済みのスレッド


def _shell() -> "win32com.client.CDispatch":
    """WScript.Shell を作る（特殊フォルダの場所を引くためだけに使う）。"""
    _ensure_com()
    return win32com.client.Dispatch("WScript.Shell")


def is_shell_link(path: Path) -> bool:
    """中身がシェルリンクかを見る。拡張子では判断しない。"""
    try:
        with path.open("rb") as handle:
            head = handle.read(20)
    except OSError:
        return False
    return (
        len(head) == 20
        and int.from_bytes(head[:4], "little") == _LINK_HEADER_SIZE
        and head[4:20] == _LINK_CLSID
    )


def search_folders() -> list[tuple[str, Path]]:
    """ショートカットを探すフォルダの一覧を返す。

    デスクトップの場所は決め打ちしない。OneDrive 同期環境では実体が
    C:\\Users\\<名前>\\OneDrive\\デスクトップ に移っており、
    Path.home() / "Desktop" では空振りするため。
    """
    shell = _shell()
    folders: list[tuple[str, Path]] = []

    for label, key in _SEARCH_FOLDERS:
        try:
            raw = shell.SpecialFolders(key)
        except Exception as exc:  # noqa: BLE001 - COM は種類が定まらない
            raise ShortcutError(f"{label} の場所を取得できませんでした: {exc}") from exc
        if not raw:
            continue
        path = Path(str(raw))
        if path.is_dir():
            folders.append((label, path))

    return folders


def resolve(lnk: Path, source: str = "") -> Shortcut:
    """ショートカットを解決して、リンク先と起動引数を取り出す。

    `WScript.Shell.CreateShortcut` は使わない。パスが `.lnk` / `.url` で
    終わっていないと COM エラーで拒否されるため、`.kww` のような固有拡張子の
    ショートカットが読めない。`IShellLink` は中身だけを見るので拡張子を問わない。
    """
    _ensure_com()
    try:
        link = pythoncom.CoCreateInstance(
            shellcon.CLSID_ShellLink,
            None,
            pythoncom.CLSCTX_INPROC_SERVER,
            shellcon.IID_IShellLink,
        )
        link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(lnk))
        # RAWPATH は保存されたままの文字列。%ProgramFiles% などが残るので展開する
        raw_target, _ = link.GetPath(shellcon.SLGP_RAWPATH)
        target = os.path.expandvars(str(raw_target))
        args = os.path.expandvars(str(link.GetArguments()))
        work_dir = os.path.expandvars(str(link.GetWorkingDirectory()))
    except Exception as exc:  # noqa: BLE001 - COM は種類が定まらない
        raise ShortcutError(f"{lnk.name} を解決できませんでした: {exc}") from exc

    if not target:
        raise ShortcutError(
            f"{lnk.name} のリンク先を取り出せませんでした。\n"
            "ファイル以外（プリンタや仮想フォルダなど）を指しています。"
        )

    return Shortcut(
        name=lnk.stem,
        lnk=lnk,
        target=Path(target),
        args=args,
        work_dir=Path(work_dir) if work_dir else None,
        source=source,
    )


def _is_excluded(shortcut: Shortcut) -> bool:
    lowered = shortcut.name.lower()
    if any(word in lowered for word in _EXCLUDE_KEYWORDS):
        return True
    return shortcut.target.suffix.lower() not in (".exe", ".bat", ".cmd", ".com")


def list_shortcuts(*, desktop_only: bool = False) -> list[Shortcut]:
    """一覧に出せるショートカットを集める。

    リンク先が存在しないもの、アンインストーラ、exe 以外を指すものは除く。
    同じ実行ファイルを指すものが複数あれば 1 つにまとめる。

    `.lnk` 以外も拾う。固有拡張子のショートカットで配られるアプリがあるため。
    """
    folders = search_folders()
    if desktop_only:
        folders = [item for item in folders if item[0].startswith("デスクトップ")]

    found: dict[str, Shortcut] = {}

    for label, folder in folders:
        # スタートメニューは階層が深いので再帰的に探す
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            # .lnk は中身を読まずに通す（大半がこれなので、先に判定して速くする）
            if path.suffix.lower() != ".lnk" and not is_shell_link(path):
                continue
            try:
                shortcut = resolve(path, source=label)
            except ShortcutError:
                continue  # 壊れたリンクは黙って飛ばす
            if not shortcut.exists or _is_excluded(shortcut):
                continue

            key = f"{shortcut.name.lower()}|{str(shortcut.target).lower()}"
            found.setdefault(key, shortcut)

    return sorted(found.values(), key=lambda s: (s.source, s.name))
