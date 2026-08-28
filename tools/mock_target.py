"""win_rpa の練習用ダミーアプリを起動する。

対象アプリが手元にない状態でも自動操作を組み立てられるようにするための、
架空の「売上管理システム」（WinForms 製）を立ち上げる。

実行方法（リポジトリ直下から）:
    python tools/mock_target.py
    python tools/mock_target.py --delay 8          # 集計に 8 秒かける
    python tools/mock_target.py --variant          # ボタン名が変わった版

中身は tools/mock_target.ps1（PowerShell + WinForms）。Tkinter では
ウィジェットが UIA からまったく見えず、練習台にならないため。

PowerShell に渡すときは -EncodedCommand（UTF-16LE の base64）を使う。
Windows PowerShell 5.1 は BOM なし UTF-8 の .ps1 を ANSI として読むため、
-File で渡すと日本語が壊れて構文エラーになる。
"""

from __future__ import annotations

import argparse
import base64
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "mock_target.ps1"

# ダミーアプリのウィンドウタイトル。検証スクリプトから参照する
WINDOW_TITLE = "売上管理システム"


def build_command(delay: int, variant: bool) -> str:
    """ps1 の先頭にパラメータを差し込んだスクリプト本文を作る。"""
    body = SCRIPT.read_text(encoding="utf-8")
    header = f"$Delay = {delay}\n$Variant = ${'true' if variant else 'false'}\n"
    return header + body


def launch(delay: int = 3, variant: bool = False) -> subprocess.Popen[bytes]:
    """ダミーアプリを起動し、そのプロセスを返す。"""
    command = build_command(delay, variant)
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    return subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-STA",
            "-EncodedCommand",
            encoded,
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="練習用ダミーアプリを起動する")
    parser.add_argument(
        "--delay", type=int, default=3, help="「集計」にかかる秒数（既定 3）"
    )
    parser.add_argument(
        "--variant",
        action="store_true",
        help="ボタン名が変わった版で起動する（アプリ更新の再現用）",
    )
    args = parser.parse_args()

    proc = launch(args.delay, args.variant)
    print(f"ダミーアプリを起動しました（PID {proc.pid}）。ウィンドウを閉じると終了します。")
    proc.wait()


if __name__ == "__main__":
    main()
