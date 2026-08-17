"""アプリを実行ファイルにまとめるビルドスクリプト。

PyInstaller の生成物（成果物・作業ファイル・spec）をすべて build/ 配下に集める。

実行方法（リポジトリ直下から、.venv を有効化した状態で）:
    python tools/build.py webpin              # 1 ファイルの exe（コンソールあり）
    python tools/build.py webpin --windowed   # コンソールを出さない配布用
    python tools/build.py webcam --onedir     # OpenCV を使うアプリはこちら推奨

出力:
    build/dist/<アプリ名>.exe   （--onedir の場合は build/dist/<アプリ名>/）
    build/work/                 中間ファイル
    build/spec/                 生成された .spec
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# リポジトリ直下（このファイルの 1 つ上）
ROOT = Path(__file__).resolve().parent.parent

APPS_DIR = ROOT / "apps"
BUILD_DIR = ROOT / "build"

# OpenCV を使うアプリは --onefile だと展開に数秒かかるため警告する
_HEAVY_APPS = ("webcam", "esp32cam")


def available_apps() -> list[str]:
    """main.py を持つアプリ名の一覧を返す。"""
    return sorted(
        path.parent.name
        for path in APPS_DIR.glob("*/main.py")
    )


def ensure_pyinstaller() -> None:
    """PyInstaller が入っていなければ導入方法を案内して終了する。"""
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "PyInstaller が見つかりません。次のコマンドで導入してください:\n"
            "    pip install pyinstaller",
            file=sys.stderr,
        )
        raise SystemExit(1)


def build(app: str, onedir: bool = False, windowed: bool = False) -> Path:
    """指定したアプリをビルドし、成果物のパスを返す。"""
    app_dir = APPS_DIR / app
    entry = app_dir / "main.py"
    if not entry.is_file():
        raise SystemExit(
            f"アプリ '{app}' が見つかりません。利用できるアプリ: "
            + ", ".join(available_apps())
        )

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        app,
        # アプリ内相対 import（from ui.main_window import ...）を解決させる
        "--paths",
        str(app_dir),
        # 生成物をすべて build/ 配下に閉じ込める
        "--distpath",
        str(BUILD_DIR / "dist"),
        "--workpath",
        str(BUILD_DIR / "work"),
        "--specpath",
        str(BUILD_DIR / "spec"),
        "--onedir" if onedir else "--onefile",
    ]
    if windowed:
        command.append("--windowed")
    command.append(str(entry))

    print("実行:", " ".join(command), "\n")
    subprocess.run(command, cwd=ROOT, check=True)

    dist = BUILD_DIR / "dist"
    return dist / app if onedir else dist / f"{app}.exe"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="アプリを実行ファイルにまとめる（成果物は build/ 配下）"
    )
    parser.add_argument("app", choices=available_apps(), help="ビルドするアプリ名")
    parser.add_argument(
        "--onedir",
        action="store_true",
        help="1 ファイルではなくフォルダ形式で出力する（起動が速い）",
    )
    parser.add_argument(
        "--windowed",
        action="store_true",
        help="コンソールウィンドウを出さない（動作確認が済んでから使う）",
    )
    args = parser.parse_args()

    ensure_pyinstaller()

    if not args.onedir and args.app in _HEAVY_APPS:
        print(
            f"[注意] {args.app} は OpenCV を含むため、--onefile では起動に数秒かかります。"
            " --onedir を検討してください。\n"
        )

    artifact = build(args.app, onedir=args.onedir, windowed=args.windowed)

    print(f"\n[完了] {artifact}")
    print(
        "[注意] 実行ファイルからデータを読み書きする場合、Path(__file__) 起点のパスは"
        "一時フォルダを指します。設定やログの保存先は sys.executable 起点に切り替える"
        "必要があります。"
    )


if __name__ == "__main__":
    main()
