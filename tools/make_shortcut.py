"""アプリをエクスプローラーから起動するためのショートカットを作る。

.py をそのままダブルクリックすると、`.venv` ではなくシステム側の Python が
使われて依存パッケージが無く失敗する。`.venv` の pythonw.exe を指す
ショートカットを作っておけば、二重クリックで正しく起動できる。

実行方法（リポジトリ直下から）:
    python tools/make_shortcut.py win_rpa
    python tools/make_shortcut.py win_rpa --desktop      デスクトップにも置く
    python tools/make_shortcut.py win_rpa --console      コンソールを出す（原因調査用）
    python tools/make_shortcut.py win_rpa --name "月次RPA"

作られる .lnk は次を指す。

    対象   : .venv\\Scripts\\pythonw.exe
    引数   : apps\\<アプリ名>\\main.py
    作業場所: リポジトリ直下（CLAUDE.md の「常にリポジトリ直下から実行」に合わせる）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import win32com.client

ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = ROOT / "apps"
VENV_SCRIPTS = ROOT / ".venv" / "Scripts"


def _shell() -> "win32com.client.CDispatch":
    return win32com.client.Dispatch("WScript.Shell")


def desktop_dir() -> Path:
    """デスクトップの場所を返す。

    OneDrive 同期環境では実体が OneDrive 配下に移っているため、
    Path.home() / "Desktop" では空振りする。
    """
    return Path(str(_shell().SpecialFolders("Desktop")))


def create(
    app: str,
    *,
    where: Path,
    name: str | None = None,
    console: bool = False,
    args: str = "",
) -> Path:
    """ショートカットを 1 つ作り、そのパスを返す。"""
    main_py = APPS_DIR / app / "main.py"
    if not main_py.is_file():
        raise SystemExit(f"{main_py} がありません。アプリ名を確認してください。")

    runner = VENV_SCRIPTS / ("python.exe" if console else "pythonw.exe")
    if not runner.is_file():
        raise SystemExit(
            f"{runner} がありません。.venv を作ってから実行してください。"
        )

    where.mkdir(parents=True, exist_ok=True)
    lnk = where / f"{name or app}.lnk"

    shortcut = _shell().CreateShortcut(str(lnk))
    shortcut.TargetPath = str(runner)
    shortcut.Arguments = f'"{main_py}"' + (f" {args}" if args else "")
    shortcut.WorkingDirectory = str(ROOT)
    shortcut.Description = f"{app} を起動する"
    # アイコンは Python のものを流用する（専用アイコンを用意するまでの間に合わせ）
    shortcut.IconLocation = f"{VENV_SCRIPTS / 'python.exe'},0"
    shortcut.Save()

    return lnk


def main() -> None:
    parser = argparse.ArgumentParser(
        description="アプリ起動用のショートカットを作る"
    )
    parser.add_argument("app", help="アプリ名（apps/ 配下のフォルダ名）")
    parser.add_argument("--name", help="ショートカットの表示名（既定はアプリ名）")
    parser.add_argument(
        "--desktop", action="store_true", help="デスクトップにも置く"
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="python.exe を使い、コンソールを出す（起動しないときの原因調査用）",
    )
    parser.add_argument("--args", default="", help="main.py に渡す引数")
    parsed = parser.parse_args()

    targets = [APPS_DIR / parsed.app]
    if parsed.desktop:
        targets.append(desktop_dir())

    for where in targets:
        lnk = create(
            parsed.app,
            where=where,
            name=parsed.name,
            console=parsed.console,
            args=parsed.args,
        )
        try:
            shown = lnk.relative_to(ROOT)
        except ValueError:
            shown = lnk
        print(f"作成: {shown}")

    print(
        "\nエクスプローラーでこの .lnk をダブルクリックすると起動します。"
        "\n起動しない場合は --console を付けて作り直し、出た例外を確認してください。"
    )


if __name__ == "__main__":
    main()
