"""アプリがデータを読み書きする場所を決める。

実行ファイル化（PyInstaller の --onefile）すると __file__ は起動のたびに作られる
一時展開フォルダを指すため、そこに保存した CSV は終了時に消える。
frozen のときは exe と同じフォルダを使う。

このモジュールは Tkinter に依存しない。
"""

from __future__ import annotations

import sys
from pathlib import Path


def data_dir() -> Path:
    """CSV ログを置くフォルダの親を返す。

    - 通常実行: apps/webpin/
    - 実行ファイル: exe と同じフォルダ
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
