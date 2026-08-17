"""最後に使ったカメラの IP アドレスを保存・復元する。

保存先はアプリフォルダ配下の settings.json（カレントディレクトリに依存させない）。
このモジュールは Tkinter に依存しない。
"""

from __future__ import annotations

import json
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings.json"

# config.txt を書き換えていない個体の初期値に合わせる
DEFAULT_HOST = "192.168.1.141"


def load_host() -> str:
    """保存済みの IP アドレスを返す。無ければ既定値を返す。"""
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_HOST

    host = data.get("host") if isinstance(data, dict) else None
    return host if isinstance(host, str) and host else DEFAULT_HOST


def save_host(host: str) -> None:
    """IP アドレスを保存する。失敗しても致命的ではないため無視する。"""
    try:
        SETTINGS_PATH.write_text(
            json.dumps({"host": host}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
