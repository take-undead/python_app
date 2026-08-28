"""シナリオの保存と読み込み（Tkinter に依存しない）。

JSON で保存するが、画面には出さない。ユーザーが触るのは
シナリオ名と手順一覧だけで、JSON は内部の保存形式として扱う。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from logic.actions import Scenario, ScenarioError

# ファイル名に使えない文字（Windows）
_UNSAFE = re.compile(r'[\\/:*?"<>|]')


def app_dir() -> Path:
    """データを置く場所を返す。

    exe 化すると __file__ 起点は壊れる。--onefile は一時フォルダへ
    自己展開するので、設定やログの保存先がそこになり終了時に消える。
    実行ファイルとして動いているときは exe の隣を使う。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def scenarios_dir() -> Path:
    path = app_dir() / "scenarios"
    path.mkdir(parents=True, exist_ok=True)
    return path


def work_root() -> Path:
    """作業フォルダ（CSV の置き場）の親。"""
    path = app_dir() / "work"
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(name: str) -> str:
    """シナリオ名をファイル名に使える形にする。"""
    cleaned = _UNSAFE.sub("_", name).strip().strip(".")
    return cleaned or "無題"


def path_for(name: str) -> Path:
    return scenarios_dir() / f"{safe_name(name)}.json"


def stored_name(path: Path) -> str | None:
    """ファイルに入っているシナリオ名を返す。読めなければ None。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    name = data.get("name")
    return name if isinstance(name, str) and name else None


def exists(name: str) -> bool:
    """同じ名前のシナリオが既に保存されているか。"""
    return path_for(name).is_file()


def list_names() -> list[str]:
    """保存済みシナリオの名前を返す。"""
    names: list[str] = []
    for path in sorted(scenarios_dir().glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = data.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def load(name: str) -> Scenario:
    """シナリオを読み込む。"""
    path = path_for(name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScenarioError(f"シナリオ「{name}」が見つかりません。") from exc
    except json.JSONDecodeError as exc:
        raise ScenarioError(
            f"シナリオ「{name}」の内容が壊れています（{exc.lineno} 行目）。"
        ) from exc
    except OSError as exc:
        raise ScenarioError(f"シナリオ「{name}」を読めませんでした: {exc}") from exc

    return Scenario.from_dict(data)


def save(scenario: Scenario) -> Path:
    """シナリオを保存する。"""
    if not scenario.name.strip():
        raise ScenarioError("シナリオ名を入力してください。")

    path = path_for(scenario.name)

    # ファイル名に使えない文字は _ に置き換えるため、「売上:A」と「売上/A」は
    # どちらも 売上_A.json になる。別のシナリオを黙って潰さないよう弾く
    if path.is_file():
        other = stored_name(path)
        if other is not None and other != scenario.name:
            raise ScenarioError(
                f"「{scenario.name}」は、既にある「{other}」と同じ"
                f"ファイル名（{path.name}）になります。別の名前にしてください。"
            )

    try:
        path.write_text(
            json.dumps(scenario.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ScenarioError(f"シナリオを保存できませんでした: {exc}") from exc
    return path


def delete(name: str) -> None:
    """シナリオを消す。"""
    try:
        path_for(name).unlink(missing_ok=True)
    except OSError as exc:
        raise ScenarioError(f"シナリオを削除できませんでした: {exc}") from exc


def rename(old_name: str, new_name: str) -> None:
    """シナリオの名前を変える（中身の name も書き換える）。"""
    scenario = load(old_name)
    scenario.name = new_name
    save(scenario)
    if safe_name(old_name) != safe_name(new_name):
        delete(old_name)
