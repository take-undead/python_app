"""カメラ 6 台分の IP アドレスを保存・復元する。

保存先はアプリフォルダ配下の settings.json（カレントディレクトリに依存させない）。
このモジュールは Tkinter に依存しない。
"""

from __future__ import annotations

import json
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings.json"

# 同時に表示するカメラの台数
CAMERA_COUNT = 6

# config.txt を書き換えていない個体の初期値。カメラ 1 がこの値で、以降 1 ずつ増える
DEFAULT_BASE_HOST = "192.168.1.141"


def default_hosts(
    base: str = DEFAULT_BASE_HOST, count: int = CAMERA_COUNT
) -> list[str]:
    """既定の IP アドレスを count 台分返す。

    base の末尾の番号を 1 ずつ増やしていく（141, 142, ... のように割り当てる）。
    base が IP アドレスの形をしていない場合は同じ値を並べる。
    """
    prefix, _, last = base.rpartition(".")
    if not prefix or not last.isdigit():
        return [base] * count

    start = int(last)
    return [f"{prefix}.{min(start + offset, 255)}" for offset in range(count)]


def load_hosts(count: int = CAMERA_COUNT) -> list[str]:
    """保存済みの IP アドレスを count 台分返す。無い分は既定値で埋める。"""
    hosts = default_hosts(count=count)

    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return hosts

    if not isinstance(data, dict):
        return hosts

    saved = data.get("hosts")
    if not isinstance(saved, list):
        # 1 台だけを扱っていた頃の形式（{"host": "..."}）は引き継がない。
        # カメラの割り当て（141 から順番）が崩れるため既定値に戻す
        return hosts

    for index, value in enumerate(saved[:count]):
        if isinstance(value, str) and value:
            hosts[index] = value
    return hosts


def save_hosts(hosts: list[str]) -> None:
    """IP アドレスを保存する。失敗しても致命的ではないため無視する。"""
    try:
        SETTINGS_PATH.write_text(
            json.dumps({"hosts": list(hosts)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
