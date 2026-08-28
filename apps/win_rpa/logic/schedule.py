"""Windows タスクスケジューラへの登録（Tkinter に依存しない）。

月 1 回のために常駐プロセスを持たない。PC 再起動やログオン後の復帰は
OS に任せる。アプリ側は schtasks を呼んで登録・削除するだけ。

前提として、UI 操作なので画面がロックされていると動かない。
ログオン状態での放置が必要で、そこは自動化しきれない。
そのぶん、実行前日の確認実行（--dry-run）を別に登録して、
壊れていることを事前に知れるようにしている。
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from logic.storage import app_dir

# タスク定義 XML の名前空間
_TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"

# 登録するタスク名の接頭辞。一覧から自分の登録分だけを拾うために使う
TASK_PREFIX = "win_rpa"


class ScheduleError(Exception):
    """タスクの登録・削除に失敗した。"""


@dataclass(frozen=True)
class Task:
    """登録済みタスク 1 件。"""

    name: str
    scenario: str
    dry_run: bool
    next_run: str
    day: int | None = None
    time_of_day: str = ""

    @property
    def kind(self) -> str:
        return "確認実行" if self.dry_run else "実行"

    @property
    def schedule(self) -> str:
        """「毎月 1 日 09:00」の形にする。"""
        if self.day is None:
            return "毎月"
        return f"毎月 {self.day} 日 {self.time_of_day}".rstrip()


def task_name(scenario: str, *, dry_run: bool = False) -> str:
    suffix = "確認" if dry_run else "実行"
    return f"{TASK_PREFIX}_{scenario}_{suffix}"


def _runner_command(scenario: str, *, dry_run: bool) -> str:
    """タスクに登録するコマンドラインを組み立てる。

    exe 化されているときは exe を直接呼ぶ。開発中は pythonw.exe を使い、
    コンソールを出さずに動かす。
    """
    args = f'--run "{scenario}"' + (" --dry-run" if dry_run else "")

    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}" {args}'

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    runner = pythonw if pythonw.is_file() else Path(sys.executable)
    return f'"{runner}" "{app_dir() / "main.py"}" {args}'


def _decode(raw: bytes) -> str:
    """schtasks の出力を文字列にする。

    このコマンドは日本語環境でも UTF-8 で出力する。コンソールの
    コードページ（cp932）で読むとタスク名が化け、シナリオ名と
    照合できなくなる。念のため cp932 にも落とせるようにしておく。
    """
    try:
        # /XML の出力には BOM が付くので utf-8-sig で読む
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp932", errors="replace")


def _run(arguments: list[str]) -> str:
    """schtasks を呼ぶ。"""
    try:
        completed = subprocess.run(["schtasks", *arguments], capture_output=True)
    except OSError as exc:
        raise ScheduleError(f"schtasks を実行できませんでした: {exc}") from exc

    stdout = _decode(completed.stdout)
    if completed.returncode != 0:
        message = (_decode(completed.stderr) or stdout).strip()
        raise ScheduleError(
            message or f"schtasks が失敗しました（{completed.returncode}）"
        )
    return stdout


def register_monthly(
    scenario: str,
    *,
    day: int,
    time_of_day: str,
    dry_run: bool = False,
) -> str:
    """毎月 決まった日時に実行するタスクを登録する。

    day は 1〜31、time_of_day は "09:00" の形式。
    同名のタスクがあれば上書きする（/F）。
    """
    if not 1 <= day <= 31:
        raise ScheduleError("日にちは 1〜31 で指定してください。")
    if len(time_of_day) != 5 or time_of_day[2] != ":":
        raise ScheduleError('時刻は "09:00" の形式で指定してください。')

    name = task_name(scenario, dry_run=dry_run)
    _run([
        "/Create", "/F",
        "/TN", name,
        "/TR", _runner_command(scenario, dry_run=dry_run),
        "/SC", "MONTHLY",
        "/D", str(day),
        "/ST", time_of_day,
    ])
    return name


def unregister(scenario: str, *, dry_run: bool = False) -> None:
    """タスクを削除する。無ければ何もしない。"""
    name = task_name(scenario, dry_run=dry_run)
    try:
        _run(["/Delete", "/F", "/TN", name])
    except ScheduleError as exc:
        if "見つかりません" in str(exc) or "cannot find" in str(exc).lower():
            return
        raise


def task_detail(name: str) -> tuple[int | None, str]:
    """登録済みタスクの「毎月何日・何時」を読み取る。

    /Query の CSV には次回実行日時しか出ないため、XML から取る。
    画面に「毎月 1 日 09:00」と出したり、名前変更のときに同じ設定で
    付け替えたりするのに使う。
    """
    try:
        xml = _run(["/Query", "/TN", name, "/XML", "ONE"])
        root = ElementTree.fromstring(xml)
    except (ScheduleError, ElementTree.ParseError):
        return None, ""

    day: int | None = None
    time_of_day = ""

    for trigger in root.iter(f"{_TASK_NS}CalendarTrigger"):
        start = trigger.find(f"{_TASK_NS}StartBoundary")
        if start is not None and start.text and "T" in start.text:
            time_of_day = start.text.split("T", 1)[1][:5]

        found = trigger.find(
            f"{_TASK_NS}ScheduleByMonth/{_TASK_NS}DaysOfMonth/{_TASK_NS}Day"
        )
        if found is not None and found.text and found.text.isdigit():
            day = int(found.text)

    return day, time_of_day


def list_tasks(*, with_detail: bool = True) -> list[Task]:
    """このアプリが登録したタスクを一覧する。

    with_detail=False にすると XML の読み取りを省く（件数だけ見たいとき）。
    """
    output = _run(["/Query", "/FO", "CSV", "/NH"])

    tasks: list[Task] = []
    for line in output.splitlines():
        fields = [item.strip('" ') for item in line.split('","')]
        if len(fields) < 3:
            continue
        full_name = fields[0].lstrip("\\").strip('"')
        if not full_name.startswith(TASK_PREFIX + "_"):
            continue

        body = full_name[len(TASK_PREFIX) + 1:]
        dry_run = body.endswith("_確認")
        scenario = body.rsplit("_", 1)[0] if "_" in body else body

        day, time_of_day = task_detail(full_name) if with_detail else (None, "")
        tasks.append(
            Task(
                name=full_name,
                scenario=scenario,
                dry_run=dry_run,
                next_run=fields[1],
                day=day,
                time_of_day=time_of_day,
            )
        )
    return sorted(tasks, key=lambda task: (task.scenario, task.dry_run))


def unregister_all(scenario: str) -> int:
    """シナリオに紐づくタスクを両方消す。消した件数を返す。

    シナリオを削除・改名したときに呼ぶ。これを怠ると、実体の無い
    シナリオを毎月起動しては失敗し続けるタスクが残る。
    """
    removed = 0
    for dry_run in (False, True):
        if is_registered(scenario, dry_run=dry_run):
            unregister(scenario, dry_run=dry_run)
            removed += 1
    return removed


def unregister_task(name: str) -> None:
    """タスク名を直接指定して消す（一覧からの削除用）。"""
    try:
        _run(["/Delete", "/F", "/TN", name])
    except ScheduleError as exc:
        if "見つかりません" in str(exc) or "cannot find" in str(exc).lower():
            return
        raise


def is_registered(scenario: str, *, dry_run: bool = False) -> bool:
    name = task_name(scenario, dry_run=dry_run)
    return any(task.name == name for task in list_tasks(with_detail=False))
