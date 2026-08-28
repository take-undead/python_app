"""デスクトップとスタートメニューのショートカットを集める（Tkinter に依存しない）。

起動するアプリを選ぶとき、exe のフルパスを人に入力させない。
ショートカットには起動引数と作業フォルダも入っているので、
exe を直接指定するより起動が安定する。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pythoncom
import win32com.client

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


def _shell() -> "win32com.client.CDispatch":
    """WScript.Shell を作る。

    ワーカースレッドから呼ばれても動くよう COM を初期化する。
    UI をふさがないよう一覧の収集は別スレッドで行うため。
    """
    try:
        pythoncom.CoInitialize()
    except pythoncom.com_error:
        pass  # 既に初期化済みのスレッド
    return win32com.client.Dispatch("WScript.Shell")


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
    """.lnk を解決して、リンク先と起動引数を取り出す。"""
    shell = _shell()
    try:
        link = shell.CreateShortcut(str(lnk))
        target = str(link.TargetPath)
        args = str(link.Arguments)
        work_dir = str(link.WorkingDirectory)
    except Exception as exc:  # noqa: BLE001 - COM は種類が定まらない
        raise ShortcutError(f"{lnk.name} を解決できませんでした: {exc}") from exc

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
    """
    folders = search_folders()
    if desktop_only:
        folders = [item for item in folders if item[0].startswith("デスクトップ")]

    found: dict[str, Shortcut] = {}

    for label, folder in folders:
        # スタートメニューは階層が深いので再帰的に探す
        for lnk in sorted(folder.rglob("*.lnk")):
            try:
                shortcut = resolve(lnk, source=label)
            except ShortcutError:
                continue  # 壊れたリンクは黙って飛ばす
            if not shortcut.exists or _is_excluded(shortcut):
                continue

            key = f"{shortcut.name.lower()}|{str(shortcut.target).lower()}"
            found.setdefault(key, shortcut)

    return sorted(found.values(), key=lambda s: (s.source, s.name))
