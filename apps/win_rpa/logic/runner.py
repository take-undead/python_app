"""手順を実行する（Tkinter に依存しない）。

対象アプリを起動し、記録された識別子で要素を探して操作する。
座標は使わない。

要素の照合は 3 段構えにしてある。アプリが更新されて AutomationId が
変わっても、名前や構造上の位置で見つかれば動き続ける。どの段で
見つかったかは実行ログに残し、上位の段が外れたら警告を出す。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from PIL import ImageGrab
from pywinauto import Application, Desktop, keyboard
from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto.uia_element_info import UIAElementInfo

from logic import appinfo, clipboard, launch, merger, picker, recorder, winfind
from logic.actions import (
    READ_MANNERS,
    Scenario,
    Step,
    build_variables,
    describe_date_range,
    describe_keys,
    expand,
    folders_at,
    menu_parts,
    requirement_error,
    resolve_date_range,
)
from logic.picker import ElementRef, describe as describe_element

# 要素が見つかるまでの待ち・確認の刻み
_POLL_SEC = 0.4

# Ctrl+C を送ってから、クリップボードに入るまで待つ上限。
# 対象アプリが応答するのを待つだけなので短くてよい
_CLIPBOARD_WAIT_SEC = 3.0

# 表示を読むとき、値のほうを先に見る種類。
# **入力欄の UIA の名前は、中身ではなく隣のラベルの文字**になることがある
# （WinForms がそう。「合計金額」というラベルの右の欄を window_text() で
# 読むと "合計金額" が返り、金額そのものは取れない）
_VALUE_FIRST_TYPES = frozenset({"Edit", "Document", "ComboBox", "Spinner"})

# 「選択してコピーする」で送る選択のしかた。上から順に試す。
# Ctrl+A は一覧や独自コントロールでは効くが、**WinForms の 1 行入力欄では
# 効かない**（実測）。効かなかったときのために、先頭から末尾までを
# 選ぶ書き方に落とす
_SELECT_KEYS: tuple[tuple[str, ...], ...] = (
    ("^a",),
    ("^{HOME}", "^+{END}"),
)

# 照合に使った手段。上ほど信頼できる
MATCH_AUTO_ID = "AutomationId"
MATCH_NAME = "名前"
MATCH_HELP = "ツールチップ"
MATCH_LEGACY = "古い形式の名前"
MATCH_PATH = "構造上の位置"

# Windows がフォルダ名に許さない文字（\ と / は階層の区切りとして通す）
_BAD_NAME_CHARS = '<>:"|?*'

# ダイアログのボタンを押すための手がかり。
# 共通ダイアログのボタンは日本語 Windows でも名前が英語のままなので、
# 先に AutomationId（IDOK = 1、IDCANCEL = 2、IDYES = 6、IDNO = 7）で探す
_DIALOG_BUTTONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "OK": (("1",), ("OK", "はい")),
    "はい": (("6",), ("はい", "Yes")),
    "いいえ": (("7",), ("いいえ", "No")),
    "キャンセル": (("2",), ("キャンセル", "Cancel")),
    "保存": (("1",), ("保存", "Save")),
    "開く": (("1",), ("開く", "Open")),
    "閉じる": (("2",), ("閉じる", "Close")),
}


class AutomationError(Exception):
    """手順の実行に失敗した。"""


def _capture_screen(path: Path) -> None:
    """画面全体を画像として保存する。

    pywinauto の capture_as_image はウィンドウ 1 つ分を撮るもので、
    Desktop には無い。画面ぜんぶを残したいので PIL で撮る。
    ダイアログが別の画面に出ていても写るよう、全モニタを対象にする。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    ImageGrab.grab(all_screens=True).save(path)


@dataclass
class StepResult:
    """手順 1 つの実行結果。実行ログにそのまま出せる形にしておく。"""

    index: int
    step: Step
    ok: bool
    message: str
    elapsed: float
    warning: str = ""
    screenshot: Path | None = None


@dataclass
class RunReport:
    """実行全体の結果。"""

    results: list[StepResult] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    @property
    def failed(self) -> StepResult | None:
        return next((r for r in self.results if not r.ok), None)


class Runner:
    """シナリオを実行する。

    on_event は進捗の通知（ワーカースレッドから呼ばれる）。
    UI 側は queue に積んで after() で拾うこと。
    """

    def __init__(
        self,
        scenario: Scenario,
        base_dir: Path,
        *,
        on_event: Callable[[str, str], None] | None = None,
        dry_run: bool = False,
        today: Any = None,
    ) -> None:
        self._scenario = scenario
        self._dry_run = dry_run
        self._on_event = on_event or (lambda kind, text: None)

        # 「今日」は実行の最初に 1 度だけ決める。差し込み変数（{yyyymm}）と
        # 更新日の絞り込みが同じ日を見るようにするため。日付をまたいで動くと、
        # 先月ぶんを集めているのに保存先だけ翌月になる、ということが起きる
        self._today = today or date.today()

        self._work_dir = scenario.resolved_work_dir(base_dir)
        self._variables = build_variables(
            self._work_dir, self._today, scenario_name=scenario.name
        )
        self._log_dir = base_dir / "logs" / datetime.now().strftime("%Y-%m")

        self._app: Application | None = None
        self._cancelled = False
        # 「フォルダを作る」で最後に作ったフォルダ。
        # 「CSV をまとめる」がここを読む。保存先（_work_dir）とは別に持つのは、
        # 「以降の保存先にする」を外していても、まとめる先は作った場所だから
        self._created_folder: Path | None = None

    # ------------------------------------------------------------------
    # 実行
    # ------------------------------------------------------------------
    def cancel(self) -> None:
        """実行中の手順が終わり次第、止める。"""
        self._cancelled = True

    def run(self, upto: int | None = None, only: int | None = None) -> RunReport:
        """手順を順に実行する。

        upto を指定すると、そこまでで止める（「ここまで実行」用）。
        only を指定すると、その手順 1 つだけを実行する（「この手順だけ実行」用）。
        **前の手順は動かさない**ので、対象アプリは自分で開いておく必要がある。
        要素は記録されたウィンドウ題名から探すため、起動済みなら見つかる。
        ただしフォルダの場所だけは手前の手順から引き継ぐ（_prime_folders）。
        """
        report = RunReport(dry_run=self._dry_run)
        self._work_dir.mkdir(parents=True, exist_ok=True)

        all_steps = self._scenario.steps
        if only is not None:
            if not 1 <= only <= len(all_steps):
                raise AutomationError(f"手順 {only} は存在しません。")
            numbered = [(only, all_steps[only - 1])]
            # 前提の判定には、シナリオ上その手前にある手順を使う。
            # 実際には動かさないが、「前にフォルダを作る手順があるか」は
            # シナリオの形の話なので、1 つだけ動かすときも同じ基準にする
            preceding = list(all_steps[: only - 1])
        else:
            numbered = list(
                enumerate(all_steps[: upto if upto is not None else None], start=1)
            )
            preceding = []

        mode = "確認実行" if self._dry_run else "実行"
        # 版を先に出す。対象 PC にコピーして動かすため、ログだけ見て
        # 「いつのコードか」が分からないと調査できない
        self._emit("info", appinfo.log_line())
        if only is not None:
            self._emit(
                "info",
                f"{mode}: 手順 {only} だけを動かします"
                "（前の手順は動かしません。対象アプリは開いておくこと）",
            )
            self._prime_folders(preceding)
        else:
            self._emit("info", f"{mode}を開始します（{len(numbered)} 手順）")

        # 前提の欠けは対象アプリを起動する前に出す。
        # 8 手順進んでから「先にフォルダを作る手順が要る」と言われても遅い
        for position, (index, step) in enumerate(numbered):
            before = preceding + [item for _, item in numbered[:position]]
            problem = requirement_error(step.spec, before, step.params)
            if not problem:
                continue
            message = f"{problem}（手順 {index}: {step.spec.label}）"
            self._emit("error", message)
            report.results.append(StepResult(index, step, False, message, 0.0))
            return report

        for index, step in numbered:
            if self._cancelled:
                self._emit("warn", "中止しました。")
                break
            if not step.enabled:
                self._emit("info", f"手順 {index}: {step.describe()} … 無効なので飛ばす")
                continue

            result = self._run_step(index, step)
            report.results.append(result)

            if not result.ok:
                break

        if report.ok and not self._cancelled:
            self._emit("ok", f"{mode}が完了しました。")
        return report

    def _prime_folders(self, steps: list[Step]) -> None:
        """1 つだけ動かすとき、手前の手順が決めるはずだったフォルダを引き継ぐ。

        操作はしないが、場所の話だけは引き継がないと成り立たない。
        「保存先にする」「フォルダを作る」を飛ばした状態で「CSV をまとめる」を
        動かすと、まとめる先が分からないまま失敗するため。
        手前の手順の指定が壊れていても、動かすのはこの 1 手順なので止めない。
        """
        work_dir, created = folders_at(
            self._work_dir, steps, scenario_name=self._scenario.name
        )
        self._created_folder = created
        # 引き継いだことは下でまとめて出すので、_set_work_dir は通さない
        self._work_dir = work_dir
        self._variables["work_dir"] = str(work_dir)

        # 保存先は無いと書き込む手順が失敗する。手前の手順を動かしていれば
        # 出来ていた場所なので、ここで用意しておく（確認実行では作らない）
        if not self._dry_run:
            try:
                self._work_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._emit("warn", f"  保存先を用意できませんでした: {exc}")

        self._emit("info", f"  保存先: {self._work_dir}（手前の手順から引き継ぎ）")
        if self._created_folder is not None:
            self._emit(
                "info", f"  まとめる先: {self._created_folder}（同上）"
            )

    def _run_step(self, index: int, step: Step) -> StepResult:
        label = f"手順 {index}: {step.describe()}"
        self._emit("step", label)
        started = time.monotonic()

        try:
            message = self._dispatch(step)
        except AutomationError as exc:
            elapsed = time.monotonic() - started
            shot = self._capture_failure(index)
            self._emit("error", f"{label} … 失敗: {exc}")
            return StepResult(index, step, False, str(exc), elapsed, screenshot=shot)

        elapsed = time.monotonic() - started
        self._emit("ok", f"{label} … {message}（{elapsed:.1f} 秒）")
        return StepResult(index, step, True, message, elapsed)

    def _dispatch(self, step: Step) -> str:
        handlers: dict[str, Callable[[Step], str]] = {
            "launch_app": self._do_launch,
            "click": self._do_click,
            "set_text": self._do_set_text,
            "check": self._do_check,
            "select": self._do_select,
            "send_keys": self._do_send_keys,
            "menu_select": self._do_menu_select,
            "focus_window": self._do_focus_window,
            "dialog_button": self._do_dialog_button,
            "save_dialog": self._do_save_dialog,
            "open_dialog": self._do_open_dialog,
            "assert_text": self._do_assert_text,
            "assert_file": self._do_assert_file,
            "screenshot": self._do_screenshot,
            "record_value": self._do_record_value,
            "merge_csv": self._do_merge_csv,
            "make_folder": self._do_make_folder,
            "set_work_dir": self._do_set_work_dir,
            "copy_files": self._do_copy_files,
            "close_app": self._do_close_app,
            "wait_seconds": self._do_wait_seconds,
            "run_program": self._do_run_program,
            "run_python": self._do_run_python,
        }
        handler = handlers.get(step.action)
        if handler is None:
            raise AutomationError(f"知らない操作です: {step.action}")

        problems = step.validate()
        if problems:
            raise AutomationError("、".join(problems))

        result = handler(step)
        self._wait_for(step)

        if step.action == "launch_app" and not self._dry_run:
            wait = step.params.get("wait_for") or {}
            if wait.get("kind") == "window" and wait.get("title"):
                self._rebind_to_window(expand(str(wait["title"]), self._variables))

        return result

    # ------------------------------------------------------------------
    # 各操作
    # ------------------------------------------------------------------
    def _do_launch(self, step: Step) -> str:
        app_info = step.params.get("app") or {}
        target = Path(str(app_info.get("target", "")))
        args = str(app_info.get("args", ""))
        work_dir = app_info.get("work_dir") or None

        if not target.is_file():
            raise AutomationError(
                f"アプリが見つかりません: {target}\n"
                "この PC では場所が違う可能性があります。選び直してください。"
            )

        # 実行ファイルでなければ、ショートカットや関連付けをたどって実体まで落とす。
        # CreateProcess はどちらも解決しないので、渡す前に exe にしておく必要がある。
        # ふだんはアプリを選んだ時点で落ちているが、この対応より前に保存された
        # シナリオと、固有拡張子のファイルを直接指定した場合のための保険
        how = "実行ファイル"
        if target.suffix.lower() not in launch.RUNNABLE_SUFFIXES:
            try:
                found = launch.resolve_launch(target)
            except launch.LaunchError as exc:
                raise AutomationError(f"起動できません: {exc}") from exc
            args = f"{found.args} {args}".strip()
            work_dir = work_dir or (str(found.work_dir) if found.work_dir else None)
            target = found.exe
            how = found.how

        if self._dry_run:
            if how == "実行ファイル":
                return f"起動できる状態です（{target.name}）"
            return f"起動できる状態です（{how} → {target.name}）"

        command = f'"{target}" {args}'.strip()
        try:
            # wait_for_idle は使わない。起動用の exe がコンソールプロセスだと
            # 「GUI プロセスでない」と言われて落ちるため。
            # 起動できたかは、この手順の「終わった合図」で判断する。
            self._app = Application(backend="uia").start(
                command,
                work_dir=str(work_dir) if work_dir else None,
                wait_for_idle=False,
            )
        except Exception as exc:  # noqa: BLE001 - pywinauto は種類が定まらない
            raise AutomationError(f"起動に失敗しました: {exc}") from exc

        return f"{app_info.get('name', target.stem)} を起動しました"

    def _rebind_to_window(self, title: str) -> None:
        """画面を持っているプロセスへ結び直す。

        起動用の exe が別プロセスの本体を立ち上げるアプリだと、
        start() が返すのはランチャー側になる。そのままだと
        「アプリを閉じる」でランチャーだけ落として本体が残る。
        """
        try:
            window = winfind.find_window(title)
            if window is None:
                return
            pid = window.wrapper_object().element_info.process_id
        except Exception:  # noqa: BLE001 - 結び直せなくても実行は続けられる
            return

        if self._app is not None and getattr(self._app, "process", None) == pid:
            return
        try:
            self._app = Application(backend="uia").connect(process=pid)
            self._emit("info", f"  画面を持つプロセス（PID {pid}）に切り替えました")
        except Exception:  # noqa: BLE001
            return

    def _do_click(self, step: Step) -> str:
        ref = self._require_element(step)
        manner = str(step.params.get("manner", "左クリック"))
        if self._dry_run:
            self._probe(ref, step)
            return "対象が見つかりました"

        control, how = self._resolve(ref, self._timeout(step))
        self._warn_if_fallback(ref, how)
        try:
            control.set_focus()
        except Exception:  # noqa: BLE001 - 前面に出せなくても押せることがある
            pass

        try:
            if manner == "ダブルクリック":
                control.double_click_input()
            elif manner == "右クリック":
                control.right_click_input()
            else:
                control.click_input()
        except Exception as exc:  # noqa: BLE001
            # マウスが届かない配置のときは UIA のパターンで押す。
            # invoke は左クリック相当なので、ほかの押し方では代用にならない
            if manner != "左クリック":
                raise AutomationError(
                    f"{describe_element(ref)} を{manner}できませんでした: {exc}"
                ) from exc
            try:
                control.invoke()
            except Exception:  # noqa: BLE001
                raise AutomationError(
                    f"{describe_element(ref)} を押せませんでした: {exc}"
                ) from exc
        return f"{manner}しました（{how} で照合）"

    def _do_set_text(self, step: Step) -> str:
        ref = self._require_element(step)
        value = expand(str(step.params.get("value", "")), self._variables)
        if self._dry_run:
            self._probe(ref, step)
            return f"対象が見つかりました（入力値: {value}）"

        control, how = self._resolve(ref, self._timeout(step))
        self._warn_if_fallback(ref, how)
        try:
            control.set_focus()
            control.set_edit_text(value)
        except Exception as exc:  # noqa: BLE001
            raise AutomationError(
                f"{describe_element(ref)} に入力できませんでした: {exc}"
            ) from exc
        return f"「{value}」を入力しました"

    def _do_check(self, step: Step) -> str:
        ref = self._require_element(step)
        want = bool(step.params.get("value", True))
        if self._dry_run:
            self._probe(ref, step)
            return "対象が見つかりました"

        control, how = self._resolve(ref, self._timeout(step))
        self._warn_if_fallback(ref, how)
        try:
            current = bool(control.get_toggle_state())
            if current != want:
                control.toggle()
        except Exception as exc:  # noqa: BLE001
            raise AutomationError(
                f"{describe_element(ref)} を切り替えられませんでした: {exc}"
            ) from exc
        return f"{'入' if want else '解除'}にしました"

    def _do_select(self, step: Step) -> str:
        ref = self._require_element(step)
        value = expand(str(step.params.get("value", "")), self._variables)
        if self._dry_run:
            self._probe(ref, step)
            return f"対象が見つかりました（選ぶ項目: {value}）"

        control, how = self._resolve(ref, self._timeout(step))
        self._warn_if_fallback(ref, how)
        try:
            control.select(value)
        except Exception as exc:  # noqa: BLE001
            raise AutomationError(
                f"{describe_element(ref)} で「{value}」を選べませんでした。"
                f"項目名が変わった可能性があります: {exc}"
            ) from exc
        return f"「{value}」を選びました"

    def _do_send_keys(self, step: Step) -> str:
        keys = str(step.params.get("keys", "")).strip()
        if not keys:
            raise AutomationError("押すキーが指定されていません。")

        repeat = max(1, int(step.params.get("repeat", 1) or 1))
        label = describe_keys(keys)
        ref = step.element()

        if self._dry_run:
            if ref is not None:
                self._probe(ref, step)
                return f"対象が見つかりました（{label} を {repeat} 回）"
            return f"{label} を {repeat} 回押す予定です"

        control: UIAWrapper | None = None
        if ref is not None:
            control, how = self._resolve(ref, self._timeout(step))
            self._warn_if_fallback(ref, how)
            try:
                control.set_focus()
            except Exception:  # noqa: BLE001 - 前面に出せなくても入ることがある
                pass

        try:
            for _ in range(repeat):
                if control is not None:
                    control.type_keys(keys)
                else:
                    # 対象を指定していないときは、いま入力を受けている場所へ送る。
                    # 直前の手順で選んだ欄にそのまま続けられるようにするため
                    keyboard.send_keys(keys)
        except Exception as exc:  # noqa: BLE001
            raise AutomationError(f"{label} を送れませんでした: {exc}") from exc

        return f"{label} を {repeat} 回押しました"

    def _do_menu_select(self, step: Step) -> str:
        parts = menu_parts(expand(str(step.params.get("path", "")), self._variables))
        if not parts:
            raise AutomationError("たどるメニューが指定されていません。")

        shown = " > ".join(parts)
        if self._dry_run:
            return f"「{shown}」をたどる予定です"

        timeout = self._timeout(step)
        for position, name in enumerate(parts):
            # 1 つめはメニューバーなので待つ価値があるが、開いたあとの項目は
            # すぐ出ている。待ち時間を短くして、綴り違いに早く気づけるようにする
            item = self._find_menu_item(name, timeout if position == 0 else 5, shown)
            try:
                item.click_input()
            except Exception as exc:  # noqa: BLE001
                raise AutomationError(
                    f"メニュー「{name}」を選べませんでした: {exc}"
                ) from exc
            time.sleep(_POLL_SEC)

        return f"「{shown}」を選びました"

    def _find_menu_item(self, name: str, timeout: float, shown: str) -> UIAWrapper:
        """メニューの項目を名前で探す。

        開いたメニューは、アプリの画面とは別の最上位ウィンドウとして出る。
        そのため、要素の照合（_resolve）ではなく、対象アプリが持つ
        ウィンドウ全部を見て回る。
        """
        deadline = time.monotonic() + timeout
        searched = False
        while True:
            windows = self._menu_windows()
            searched = searched or bool(windows)

            for window in windows:
                try:
                    for control in window.descendants(control_type="MenuItem"):
                        if control.window_text() == name:
                            return control
                except Exception:  # noqa: BLE001 - 閉じた直後のウィンドウは飛ばす
                    continue

            if time.monotonic() >= deadline:
                break
            time.sleep(_POLL_SEC)

        if not searched:
            raise AutomationError(
                "メニューをたどる対象のアプリがありません。\n"
                "先に「アプリを起動する」か「ウィンドウを前面に出す」の手順を"
                "入れてください。"
            )
        raise AutomationError(
            f"メニュー「{name}」が見つかりませんでした（たどる順: {shown}）。\n"
            "名前が変わったか、そこまで開けていない可能性があります。"
        )

    def _menu_windows(self) -> list[Any]:
        """メニューを探す対象のウィンドウ。

        探す範囲をプロセス単位に絞る。デスクトップ全体を走査すると、
        画面上のすべてのアプリの要素をたどることになり終わらない。

        起動したプロセスが 1 つもウィンドウを持たないことがある
        （ランチャー型のアプリ。メモ帳がこの形）。そのときだけ、いま
        手前にあるウィンドウのプロセスに広げる。無条件に広げると、
        対象アプリの起動に失敗したときに**関係の無いアプリのメニューを
        操作してしまう**ので、アプリを掴んでいることを条件にする。
        """
        if self._app is None:
            return []

        windows = self._windows_of(getattr(self._app, "process", None))
        if windows:
            return windows

        handle = winfind.foreground_window()
        if handle is None:
            return []
        pid = winfind.process_of(handle)
        # 自分（この操作画面）の中を探しても意味がない
        return [] if pid == os.getpid() else self._windows_of(pid)

    @staticmethod
    def _windows_of(pid: int | None) -> list[Any]:
        """そのプロセスが持つ最上位ウィンドウ。開いたメニューもここに出る。"""
        if not pid:
            return []
        windows: list[Any] = []
        for hwnd in winfind.find_windows(pid=int(pid)):
            try:
                windows.append(winfind.spec(hwnd))
            except OSError:
                continue
        return windows

    def _do_focus_window(self, step: Step) -> str:
        title = expand(str(step.params.get("title", "")), self._variables)
        timeout = self._timeout(step, default=30)

        if self._dry_run:
            state = "出ています" if winfind.find_windows(title=title) else "まだ出ていません"
            return f"「{title}」は今 {state}"

        hwnd = self._wait_window_handle(title, timeout)
        try:
            winfind.spec(hwnd).wrapper_object().set_focus()
        except Exception as exc:  # noqa: BLE001
            raise AutomationError(
                f"「{title}」を前面に出せませんでした: {exc}"
            ) from exc

        # 以降の手順が同じウィンドウを対象にできるよう、
        # そのウィンドウを持っているプロセスへ結び直す
        self._rebind_to_window(title)
        return f"「{title}」を前面に出しました"

    def _do_dialog_button(self, step: Step) -> str:
        title = expand(str(step.params.get("dialog_title", "")), self._variables).strip()
        button = str(step.params.get("button", "OK"))
        optional = bool(step.params.get("optional", True))
        timeout = self._timeout(step, default=10)

        where = f"「{title}」" if title else "出ているメッセージ"
        if self._dry_run:
            return f"{where}の「{button}」を押す予定です"

        auto_ids, names = _DIALOG_BUTTONS.get(button, ((), (button,)))

        deadline = time.monotonic() + timeout
        dialog: Any = None
        while True:
            dialog = self._find_dialog(title)
            if dialog is not None:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(_POLL_SEC)

        if dialog is None:
            if optional:
                return f"{where}は出ていなかったので飛ばしました"
            raise AutomationError(
                f"{where}が {timeout} 秒以内に出ませんでした。"
            )

        if not self._click_dialog_button(dialog, auto_ids, names):
            raise AutomationError(
                f"{where}に「{button}」ボタンが見つかりませんでした。"
            )
        return f"{where}の「{button}」を押しました"

    def _find_dialog(self, title: str) -> Any:
        """応じる対象のダイアログを探す。題名が空なら、出ているものを使う。"""
        pid = getattr(self._app, "process", None) if self._app is not None else None
        if title:
            # 共通ダイアログ（#32770）を先に見るが、アプリが自前で作った
            # 確認画面はふつうのウィンドウなので、題名が分かっているなら
            # そちらも受け入れる
            return winfind.find_dialog(title, pid) or winfind.find_window(title, pid)
        # 題名を決めていないときに、自分（この操作画面）のダイアログを
        # 押してしまわないよう除く
        return winfind.find_any_dialog(pid, exclude_pid=os.getpid())

    def _do_save_dialog(self, step: Step) -> str:
        path = self._resolve_path(str(step.params.get("path", "")))
        title = expand(
            str(step.params.get("dialog_title", "名前を付けて保存")), self._variables
        )
        timeout = self._timeout(step, default=30)

        if self._dry_run:
            return f"保存先: {path}"

        path.parent.mkdir(parents=True, exist_ok=True)
        hwnd = self._wait_window_handle(title, timeout)

        self._set_dialog_filename(hwnd, path)

        # IDOK = 1。日本語 Windows でもボタン名は Save のままなので
        # AutomationId を先に試す
        if not self._click_dialog_button(
            winfind.spec(hwnd), ("1",), ("保存", "Save", "OK")
        ):
            raise AutomationError(
                "保存ダイアログの「保存」ボタンが見つかりませんでした。"
            )

        # 上書き確認が出ることがある
        self._dismiss_overwrite_prompt()
        return f"{path.name} に保存しました"

    def _do_open_dialog(self, step: Step) -> str:
        path = self._resolve_path(str(step.params.get("path", "")))
        title = expand(str(step.params.get("dialog_title", "開く")), self._variables)
        timeout = self._timeout(step, default=30)

        if self._dry_run:
            state = "あります" if path.is_file() else "まだありません"
            return f"開く予定: {path}（{state}）"

        if not path.is_file():
            raise AutomationError(
                f"開くファイルがありません: {path}\n"
                "前の手順で作られるはずのファイルなら、そこが失敗しています。"
            )

        hwnd = self._wait_window_handle(title, timeout)
        self._set_dialog_filename(hwnd, path)

        # IDOK = 1。共通ダイアログのボタン名は日本語 Windows でも Open のまま
        if not self._click_dialog_button(
            winfind.spec(hwnd), ("1",), ("開く", "Open", "OK")
        ):
            raise AutomationError("開くダイアログの「開く」ボタンが見つかりませんでした。")
        return f"{path.name} を開きました"

    def _set_dialog_filename(self, hwnd: int, path: Path) -> None:
        """保存ダイアログの「ファイル名」欄にパスを入れる。

        UIA の SetValue は共通ダイアログ相手だと COM エラー（タイムアウト /
        取り消し）で失敗するため、win32 の WM_SETTEXT 経由で入れる。

        ダイアログにはアドレスバーや検索欄など Edit が複数あり、どれが
        ファイル名欄かは Windows の版で変わる（Windows 11 は control_id が
        1001、以前は 1148）。入れたあとに読み返して、入っていなければ
        次の候補を試す。
        """
        dialog = winfind.spec_win32(hwnd)
        wanted = str(path)
        last_error = ""

        for spec in (
            dialog.child_window(control_id=1001, class_name="Edit"),
            dialog.child_window(control_id=1148, class_name="Edit"),
            dialog.child_window(class_name="Edit", found_index=0),
        ):
            try:
                if not spec.exists(timeout=1):
                    continue
                edit = spec.wrapper_object()
                edit.set_edit_text(wanted)
                if edit.window_text() == wanted:
                    return
                last_error = "入力しても値が入りませんでした"
            except Exception as exc:  # noqa: BLE001 - 次の候補を試す
                last_error = str(exc)
                continue

        raise AutomationError(
            "保存ダイアログにファイル名を入れられませんでした"
            + (f": {last_error}" if last_error else "。")
        )

    def _do_assert_text(self, step: Step) -> str:
        ref = self._require_element(step)
        expected = expand(str(step.params.get("contains", "")), self._variables)
        timeout = self._timeout(step)

        if self._dry_run:
            self._probe(ref, step)
            return f"対象が見つかりました（期待: {expected}）"

        deadline = time.monotonic() + timeout
        actual = ""
        while time.monotonic() < deadline:
            control, _ = self._resolve(ref, timeout=2)
            actual = self._text_of(control)
            if expected in actual:
                return f"「{expected}」を確認しました"
            time.sleep(_POLL_SEC)

        raise AutomationError(
            f"{describe_element(ref)} に「{expected}」が現れませんでした。"
            f"実際の表示: 「{actual}」"
        )

    def _do_assert_file(self, step: Step) -> str:
        path = self._resolve_path(str(step.params.get("path", "")))
        min_rows = int(step.params.get("min_rows", 2) or 0)
        timeout = self._timeout(step, default=60)

        if self._dry_run:
            return f"確認予定: {path}"

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.is_file() and path.stat().st_size > 0:
                break
            time.sleep(_POLL_SEC)
        else:
            raise AutomationError(
                f"{path} ができませんでした。"
                "対象アプリが出力に失敗した可能性があります。"
            )

        try:
            rows = merger.count_rows(path)
        except merger.MergeError as exc:
            raise AutomationError(str(exc)) from exc

        if rows < min_rows:
            raise AutomationError(
                f"{path.name} が {rows} 行しかありません（最低 {min_rows} 行）。"
                "対象アプリの出力が空の可能性があります。"
            )
        return f"{path.name}（{rows} 行）を確認しました"

    def _do_screenshot(self, step: Step) -> str:
        raw = expand(str(step.params.get("name", "")), self._variables).strip()
        name = Path(raw.replace("\\", "/")).name
        if not name:
            raise AutomationError("画像の名前が指定されていません。")
        if not name.lower().endswith(".png"):
            name += ".png"

        path = self._work_dir / name
        if self._dry_run:
            return f"保存予定: {path}"

        try:
            _capture_screen(path)
        except OSError as exc:
            raise AutomationError(f"画面を保存できませんでした: {exc}") from exc
        return f"{path} に保存しました"

    def _do_record_value(self, step: Step) -> str:
        """画面に出ている数値を、記録用の CSV の末尾に 1 行足す。

        対象アプリが CSV を吐いてくれない値（画面にしか出ない集計結果）を
        貯めるための手順。動かすたびに 1 行増える。
        """
        ref = self._require_element(step)
        manner = str(step.params.get("how", READ_MANNERS[0]))
        label = expand(str(step.params.get("label", "")), self._variables).strip()
        number_only = bool(step.params.get("number_only", True))
        path = self._record_path(step)

        if self._dry_run:
            # クリップボードは触らない。確認実行が人の作業中に走ることがあり、
            # 操作しないはずの実行で中身を消すわけにはいかない
            self._probe(ref, step)
            return (
                f"「{label}」を {path} に 1 行足す予定です"
                f"（今 {recorder.row_count(path)} 行）"
            )

        control, how = self._resolve(ref, self._timeout(step))
        self._warn_if_fallback(ref, how)

        raw = self._read_value(control, manner, ref).strip()
        value = raw
        if number_only:
            value = recorder.to_number(raw)
            if not value:
                raise AutomationError(
                    f"{describe_element(ref)} の表示「{raw}」から数値を"
                    "取り出せませんでした。\n"
                    "数字が出ていない場所を指しているか、処理がまだ終わって"
                    "いない可能性があります。数字以外を記録したいときは"
                    "「数値だけを取り出す」を外してください。"
                )
            if value != raw:
                self._emit("info", f"  読み取り: 「{raw}」 → {value}")

        try:
            rows = recorder.append(
                path,
                scenario=self._scenario.name,
                label=label,
                value=value,
                raw=raw,
            )
        except recorder.RecordError as exc:
            raise AutomationError(str(exc)) from exc

        return f"「{label}」に {value} を記録しました（{path} の {rows} 行目）"

    def _record_path(self, step: Step) -> Path:
        """記録先のファイル。拡張子が無ければ .csv を付ける。"""
        raw = str(step.params.get("file", "")).strip()
        if not raw:
            raise AutomationError("記録するファイルが指定されていません。")
        if not raw.lower().endswith(".csv"):
            raw += ".csv"
        return self._resolve_path(raw)

    def _read_value(
        self, control: UIAWrapper, manner: str, ref: ElementRef
    ) -> str:
        """画面に出ている値を読む。読み取り方は手順が持っている。

        表示から読むときは、種類によって「名前」と「値」のどちらを先に
        見るかを変える。入力欄は名前が隣のラベルの文字になることがあり、
        名前を先に読むとラベルのほうを記録してしまう。
        """
        if manner != READ_MANNERS[0]:
            return self._copy_value(control, manner, ref)

        name_text = self._text_of(control)
        value_text = self._value_of(control)
        if ref.control_type in _VALUE_FIRST_TYPES:
            name_text, value_text = value_text, name_text

        text = name_text if name_text.strip() else value_text
        if not text.strip():
            raise AutomationError(
                f"{describe_element(ref)} から文字を読めませんでした。\n"
                "表示を公開していない場所の可能性があります。"
                "「読み取り方」を「選択してコピーする」に変えて試してください。"
            )
        return text

    def _copy_value(
        self, control: UIAWrapper, manner: str, ref: ElementRef
    ) -> str:
        """対象アプリにコピーさせて、クリップボードから受け取る。

        UIA から読めない表示（独自描画の欄、一覧のセル）のための道。
        全選択が効かない場所もあるので、クリックしてから Ctrl+C だけを
        送る形も選べるようにしてある（一覧のセルはこちら。全選択すると
        表ぜんぶがコピーされてしまう）。
        """
        try:
            control.set_focus()
        except Exception:  # noqa: BLE001 - 前面に出せなくてもコピーできることがある
            pass

        if manner == READ_MANNERS[2]:
            try:
                control.click_input()
            except Exception as exc:  # noqa: BLE001
                raise AutomationError(
                    f"{describe_element(ref)} をクリックできませんでした: {exc}"
                ) from exc
            time.sleep(_POLL_SEC)
            attempts: tuple[tuple[str, ...], ...] = ((),)
        else:
            attempts = _SELECT_KEYS

        for number, select_keys in enumerate(attempts, start=1):
            last = number == len(attempts)
            text = self._copy_once(
                control, ref, select_keys,
                _CLIPBOARD_WAIT_SEC if last else 1.0,
            )
            if not text.strip():
                continue
            if number > 1:
                self._emit(
                    "info",
                    "  Ctrl+A では選択できなかったので、"
                    "先頭から末尾までを選んでコピーしました。",
                )
            return text

        raise AutomationError(
            f"{describe_element(ref)} をコピーできませんでした"
            "（クリップボードが空のままです）。\n"
            "選択できない場所の可能性があります。「読み取り方」を"
            "「表示から読む」に変えるか、手前に「キーを押す」で選ぶ手順を"
            "入れてください。"
        )

    def _copy_once(
        self,
        control: UIAWrapper,
        ref: ElementRef,
        select_keys: tuple[str, ...],
        wait: float,
    ) -> str:
        """選択してコピーし、クリップボードに入るまで待つ。空なら空文字。"""
        # 送る前に空にする。コピーが効かなかったときに、前から入っていた
        # 文字を「読み取った値」として記録してしまわないため
        try:
            clipboard.clear()
        except clipboard.ClipboardError as exc:
            raise AutomationError(str(exc)) from exc

        try:
            for keys in select_keys:
                control.type_keys(keys)
                time.sleep(_POLL_SEC)
            control.type_keys("^c")
        except Exception as exc:  # noqa: BLE001
            raise AutomationError(
                f"{describe_element(ref)} にコピーの操作を送れませんでした: {exc}"
            ) from exc

        deadline = time.monotonic() + wait
        while True:
            try:
                text = clipboard.read_text()
            except clipboard.ClipboardError as exc:
                raise AutomationError(str(exc)) from exc
            if text.strip():
                return text
            if time.monotonic() >= deadline:
                return ""
            time.sleep(_POLL_SEC)

    def _do_merge_csv(self, step: Step) -> str:
        """指定したフォルダの CSV を全部まとめ、日付順に並べて同じ場所に置く。

        対象を選ばせず「そのフォルダの CSV 全部」に決め打ちにしてある。
        まとめる先のフォルダに入っているものが対象、という以外の
        解釈が無いため。
        """
        raw = str(step.params.get("folder", "")).strip()
        if raw:
            folder = self._resolve_path(raw)
        else:
            # 手前の「フォルダを作る」で作った場所。それが無ければ
            # その時点の保存先（「保存先フォルダを選ぶ」だけ置いた場合）
            folder = self._created_folder or self._work_dir

        name = expand(str(step.params.get("output", "")), self._variables).strip()
        name = Path(name.replace("\\", "/")).name
        if not name:
            raise AutomationError("できあがるファイル名が指定されていません。")
        if not name.lower().endswith(".csv"):
            name += ".csv"

        output = folder / name
        add_source = bool(step.params.get("add_source", True))
        min_rows = int(step.params.get("min_rows", 2) or 0)

        try:
            # 前回の出力を取り込んで行が倍になるのを避けるため output は除く
            sources = merger.find_sources(folder, exclude=output)
        except merger.MergeError as exc:
            if self._dry_run:
                return f"（確認実行のため対象なし: {exc}）"
            raise AutomationError(str(exc)) from exc

        if self._dry_run:
            return f"{folder} の {len(sources)} 件を {name} にまとめる予定です"

        try:
            result = merger.merge(
                sources, output, add_source=add_source, min_rows=min_rows,
                sort_by_date=True,
            )
        except merger.MergeError as exc:
            raise AutomationError(str(exc)) from exc

        if result.added_columns:
            self._emit(
                "warn",
                "  ファイルごとに列が違います。後から足した列: "
                + "、".join(result.added_columns),
            )

        if result.sort_column:
            self._emit("info", f"  「{result.sort_column}」の古い順に並べ替えました")

            # 1 か月ぶんが揃っているかを、実行ログだけで確かめられるようにする
            if result.first_time is not None and result.last_time is not None:
                self._emit(
                    "info",
                    f"  {self._stamp(result.first_time)} 〜 "
                    f"{self._stamp(result.last_time)} の {result.row_count} 行",
                )
            if result.unsorted_rows:
                self._emit(
                    "warn",
                    f"  {result.unsorted_rows} 行は日時として読めなかったので"
                    "末尾に置きました。",
                )
        else:
            self._emit(
                "warn",
                "  先頭付近に日時の列が見つからなかったので、"
                "読み込んだ順のままにしました。",
            )

        return (
            f"{result.source_count} 件を {result.row_count} 行にまとめました"
            f"（{output}）"
        )

    # ------------------------------------------------------------------
    # ファイル操作
    # ------------------------------------------------------------------
    def _folder_target(self, step: Step) -> Path:
        """「フォルダを作る」がどこを指しているかを決める（作りはしない）。"""
        name = self._folder_name(str(step.params.get("name", "")))
        parent_raw = str(step.params.get("parent", "")).strip()
        parent = self._resolve_path(parent_raw) if parent_raw else self._work_dir

        target = Path(name)
        return target if target.is_absolute() else parent / target

    def _do_make_folder(self, step: Step) -> str:
        folder = self._folder_target(step)
        set_as_work = bool(step.params.get("set_as_work", True))

        # 「CSV をまとめる」がここを読む。確認実行でも覚えておく
        self._created_folder = folder

        if self._dry_run:
            # 作りはしないが、以降の手順の基準は追随させる。そうしないと
            # 確認実行のときだけ保存先が食い違う
            state = "既にあります" if folder.is_dir() else "この名前で作ります"
            if set_as_work:
                self._set_work_dir(folder)
            return f"{folder}（{state}）"

        existed = folder.is_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AutomationError(
                f"フォルダを作れませんでした: {folder}\n{exc}"
            ) from exc

        if set_as_work:
            self._set_work_dir(folder)
        return f"{folder} を{'確認しました' if existed else '作りました'}"

    def _do_set_work_dir(self, step: Step) -> str:
        folder = self._resolve_path(str(step.params.get("path", "")))
        create = bool(step.params.get("create", True))

        if self._dry_run:
            # 「フォルダを作る」を飛ばしているので、無くても失敗にしない
            note = (
                "あります"
                if folder.is_dir()
                else ("無いので実行時に作ります" if create else "まだありません")
            )
            self._set_work_dir(folder)
            return f"{folder}（{note}）"

        if not folder.is_dir():
            if not create:
                raise AutomationError(f"フォルダがありません: {folder}")
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise AutomationError(
                    f"フォルダを作れませんでした: {folder}\n{exc}"
                ) from exc

        self._set_work_dir(folder)
        return f"保存先を {folder} にしました"

    def _do_copy_files(self, step: Step) -> str:
        pattern = expand(str(step.params.get("source", "")), self._variables).strip()
        from_raw = str(step.params.get("from_dir", "")).strip()
        source_dir = self._resolve_path(from_raw) if from_raw else self._work_dir
        dest = self._resolve_path(str(step.params.get("dest", "")))
        move = str(step.params.get("mode", "コピー")) == "移動"
        rename = expand(str(step.params.get("rename", "")), self._variables).strip()
        on_exists = str(step.params.get("on_exists", "飛ばす"))
        min_count = int(step.params.get("min_count", 1) or 0)
        verb = "移動" if move else "コピー"

        if not source_dir.is_dir():
            if self._dry_run:
                return f"（確認実行: {source_dir} はまだありません）"
            raise AutomationError(f"探す場所がありません: {source_dir}")

        try:
            sources = sorted(p for p in source_dir.glob(pattern) if p.is_file())
        except (NotImplementedError, ValueError, OSError) as exc:
            # フルパスや空文字を入れると glob が例外を投げる。
            # 「想定外のエラー」で終わらせず、何が悪いかを出す
            raise AutomationError(
                f"「{pattern}」はファイルの選び方として使えません。"
                "［型］から選び直してください。\n"
                f"（{exc}）"
            ) from exc

        found = len(sources)
        sources, when = self._filter_by_modified(sources, step)
        narrowed = (
            f"、更新日が {when} のものは {len(sources)} 件" if when else ""
        )

        if self._dry_run:
            return (
                f"{source_dir} の「{pattern}」は今 {found} 件{narrowed}。"
                f"{dest} へ{verb}する予定です"
            )

        if when:
            self._emit(
                "info",
                f"  「{pattern}」は {found} 件。"
                f"そのうち更新日が {when} のものは {len(sources)} 件です。",
            )

        if len(sources) < min_count:
            raise AutomationError(
                f"{source_dir} に「{pattern}」に合うファイルが {len(sources)} 件しか"
                f"ありません（最低 {min_count} 件）。"
                + (
                    f"\n更新日を「{when}」で絞っています"
                    f"（絞る前は {found} 件）。範囲が合っているか確かめてください。"
                    if when
                    else "対象アプリが出力に失敗した可能性があります。"
                )
            )

        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AutomationError(f"行き先を作れませんでした: {dest}\n{exc}") from exc

        moved = 0
        skipped = 0
        for number, source in enumerate(sources, start=1):
            target = dest / self._copied_name(source, rename, number, len(sources))

            if target.exists():
                if on_exists == "飛ばす":
                    skipped += 1
                    continue
                if on_exists == "エラーにする":
                    raise AutomationError(
                        f"{target} が既にあります。"
                        "「行き先に同じ名前があるとき」の設定を見直してください。"
                    )
            if source.resolve() == target.resolve():
                skipped += 1
                continue

            try:
                if move:
                    if target.exists():
                        target.unlink()
                    shutil.move(str(source), str(target))
                else:
                    shutil.copy2(source, target)
            except OSError as exc:
                raise AutomationError(
                    f"{source.name} を{verb}できませんでした: {exc}"
                ) from exc
            moved += 1

        if skipped:
            self._emit("warn", f"  同じ名前があったので {skipped} 件を飛ばしました。")
        return f"{moved} 件を {dest} へ{verb}しました"

    def _filter_by_modified(
        self, sources: list[Path], step: Step
    ) -> tuple[list[Path], str]:
        """更新日で対象を絞る。絞った一覧と、範囲の言い方を返す。

        ファイル名に日付が入っていない出力を「先月ぶんだけ」運ぶために使う。
        範囲は選ばせるので、ここに来るのは解決済みの型だけ。
        """
        setting = step.params.get("modified")
        when = describe_date_range(setting, self._today)
        if not when:
            return sources, ""

        start, end = resolve_date_range(setting, self._today)
        if start is None or end is None:
            return sources, ""

        kept: list[Path] = []
        for path in sources:
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime)
            except OSError:
                # 読めないものは落とさず残す。数が合わなければ
                # 「最低件数」で止まるので、ここで黙って消すほうが危ない
                kept.append(path)
                continue
            if start <= modified <= end:
                kept.append(path)
        return kept, when

    @staticmethod
    def _copied_name(source: Path, rename: str, number: int, total: int) -> str:
        """行き先でのファイル名を決める。

        付け替える名前に拡張子が無ければ元の拡張子を引き継ぐ。2 件以上を
        同じ名前にすると上書きし合うので、そのときだけ連番を足す。
        """
        if not rename:
            return source.name

        suffix = Path(rename).suffix
        base = rename[: len(rename) - len(suffix)] if suffix else rename
        if total > 1:
            base = f"{base}_{number:02d}"
        return base + (suffix or source.suffix)

    def _folder_name(self, raw: str) -> str:
        """フォルダ名の差し込みを展開し、使えない文字が無いか確かめる。"""
        name = expand(raw, self._variables).strip().strip("\\/")
        if not name:
            raise AutomationError("フォルダ名が空です。")

        # ドライブ文字のコロンは通す（絶対パスを直に指定できるようにするため）
        checked = name[2:] if len(name) > 1 and name[1] == ":" else name
        bad = sorted({c for c in checked if c in _BAD_NAME_CHARS})
        if bad:
            raise AutomationError(
                f"フォルダ名に使えない文字が入っています: {' '.join(bad)}\n"
                f"（{name}）"
            )
        return name

    def _set_work_dir(self, folder: Path) -> None:
        """以降の手順の基準フォルダを差し替える。

        相対指定のファイルは _resolve_path でここを基準に解決するので、
        差し込み変数の {work_dir} も一緒に更新しないと食い違う。
        """
        self._work_dir = folder
        self._variables["work_dir"] = str(folder)
        self._emit("info", f"  以降の保存先: {folder}")

    # ------------------------------------------------------------------
    def _do_close_app(self, step: Step) -> str:
        if self._dry_run or self._app is None:
            return "閉じる対象はありません"
        try:
            self._app.kill(soft=True)
        except Exception:  # noqa: BLE001 - 既に閉じていることがある
            pass
        self._app = None
        return "閉じました"

    def _do_wait_seconds(self, step: Step) -> str:
        try:
            seconds = max(0, int(step.params.get("seconds", 0) or 0))
        except (TypeError, ValueError):
            raise AutomationError("待つ秒数が数値ではありません。") from None

        if self._dry_run:
            return f"{seconds} 秒待つ予定です"

        # 一息に sleep せず刻む。［中止］を押してから最大 5 分待たされないため
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._cancelled:
                return f"{seconds} 秒待つ途中で中止しました"
            time.sleep(min(_POLL_SEC, max(0.0, deadline - time.monotonic())))
        return f"{seconds} 秒待ちました"

    def _do_run_program(self, step: Step) -> str:
        app_info = step.params.get("app") or {}
        target = Path(str(app_info.get("target", "")))
        args = expand(str(app_info.get("args", "")), self._variables)
        wait_exit = bool(step.params.get("wait_exit", True))
        timeout = self._timeout(step, default=300)
        work_dir = app_info.get("work_dir") or str(self._work_dir)

        if not target.is_file():
            raise AutomationError(
                f"プログラムが見つかりません: {target}\n"
                "この PC では場所が違う可能性があります。選び直してください。"
            )

        # ショートカットや関連付けは CreateProcess が解決しないので、
        # 「アプリを起動する」と同じ手順で実体まで落とす
        if target.suffix.lower() not in launch.RUNNABLE_SUFFIXES:
            try:
                found = launch.resolve_launch(target)
            except launch.LaunchError as exc:
                raise AutomationError(f"実行できません: {exc}") from exc
            args = f"{found.args} {args}".strip()
            work_dir = str(found.work_dir) if found.work_dir else work_dir
            target = found.exe

        if self._dry_run:
            return f"実行できる状態です（{target.name}）"

        command = [str(target), *args.split()]
        if not wait_exit:
            try:
                subprocess.Popen(command, cwd=work_dir)
            except OSError as exc:
                raise AutomationError(f"{target.name} を実行できませんでした: {exc}") from exc
            return f"{target.name} を起動しました（終わるのは待ちません）"

        try:
            completed = subprocess.run(
                command,
                cwd=work_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise AutomationError(
                f"{target.name} が {timeout} 秒で終わりませんでした。"
            ) from exc
        except OSError as exc:
            raise AutomationError(f"{target.name} を実行できませんでした: {exc}") from exc

        if completed.returncode != 0:
            raise AutomationError(
                f"{target.name} が失敗しました（終了コード "
                f"{completed.returncode}）:\n{completed.stderr.strip()[:500]}"
            )
        return f"{target.name} を実行しました"

    def _do_run_python(self, step: Step) -> str:
        script = self._resolve_path(str(step.params.get("script", "")))
        args = expand(str(step.params.get("args", "")), self._variables)
        timeout = self._timeout(step, default=300)

        if not script.is_file():
            raise AutomationError(f"スクリプトが見つかりません: {script}")
        if self._dry_run:
            return f"実行予定: {script.name}"

        command = [sys.executable, str(script)]
        if args:
            command.extend(args.split())

        try:
            completed = subprocess.run(
                command,
                cwd=str(self._work_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise AutomationError(
                f"{script.name} が {timeout} 秒で終わりませんでした。"
            ) from exc

        if completed.returncode != 0:
            raise AutomationError(
                f"{script.name} が失敗しました（終了コード "
                f"{completed.returncode}）:\n{completed.stderr.strip()[:500]}"
            )
        return f"{script.name} を実行しました"

    # ------------------------------------------------------------------
    # 要素の照合（3 段構え）
    # ------------------------------------------------------------------
    def _resolve(self, ref: ElementRef, timeout: float) -> tuple[UIAWrapper, str]:
        """記録された識別子で要素を探す。見つけた手段も返す。"""
        window = self._window_for(ref, timeout)
        deadline = time.monotonic() + timeout
        last_error = ""

        while True:
            for finder, how in (
                (self._find_by_auto_id, MATCH_AUTO_ID),
                (self._find_by_name, MATCH_NAME),
                (self._find_by_help_text, MATCH_HELP),
                (self._find_by_legacy_name, MATCH_LEGACY),
                (self._find_by_path, MATCH_PATH),
            ):
                try:
                    control = finder(window, ref)
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
                    control = None
                if control is not None:
                    return control, how

            if time.monotonic() >= deadline:
                break
            time.sleep(_POLL_SEC)

        raise AutomationError(
            f"{describe_element(ref)} が見つかりませんでした。\n"
            "対象アプリが更新されたか、まだ画面が出ていない可能性があります。"
            "［要素を選び直す］で指定し直してください。"
            + (f"\n（詳細: {last_error}）" if last_error else "")
        )

    def _find_by_auto_id(self, window: Any, ref: ElementRef) -> UIAWrapper | None:
        if not ref.auto_id:
            return None
        spec = window.child_window(auto_id=ref.auto_id)
        if ref.control_type:
            spec = window.child_window(
                auto_id=ref.auto_id, control_type=ref.control_type
            )
        return spec.wrapper_object() if spec.exists() else None

    def _find_by_name(self, window: Any, ref: ElementRef) -> UIAWrapper | None:
        if not ref.name:
            return None
        spec = window.child_window(title=ref.name, control_type=ref.control_type)
        return spec.wrapper_object() if spec.exists() else None

    def _find_by_help_text(self, window: Any, ref: ElementRef) -> UIAWrapper | None:
        """ツールチップで探す。アイコンだけのボタン用。"""
        return self._find_by_property(window, ref, ref.help_text, picker.help_text_of)

    def _find_by_legacy_name(self, window: Any, ref: ElementRef) -> UIAWrapper | None:
        """古い形式（LegacyIAccessible）の名前で探す。"""
        return self._find_by_property(
            window, ref, ref.legacy_name, picker.legacy_name_of
        )

    def _find_by_property(
        self,
        window: Any,
        ref: ElementRef,
        wanted: str,
        read: Callable[[UIAElementInfo], str],
    ) -> UIAWrapper | None:
        """child_window で指定できない属性は、自分で見て回る。

        auto_id と名前が外れたときだけ動くので、走査の重さは許容する。
        """
        if not wanted:
            return None
        for control in window.descendants(control_type=ref.control_type or None):
            if read(control.element_info) == wanted:
                return control
        return None

    def _find_by_path(self, window: Any, ref: ElementRef) -> UIAWrapper | None:
        """AutomationId も名前も変わったときの最後の手段。

        兄弟全体の通し番号ではなく「同じ種類の中で何番目か」でたどる。
        装飾用の要素が増えてもずれにくくするため。
        """
        if not ref.index_path:
            return None

        info: UIAElementInfo = window.wrapper_object().element_info
        for control_type, index in ref.index_path:
            matches = [
                child
                for child in info.children()
                if str(child.control_type) == control_type
            ]
            if index >= len(matches):
                return None
            info = matches[index]
        return UIAWrapper(info)

    def _warn_if_fallback(self, ref: ElementRef, how: str) -> None:
        # 位置での照合は、名前が無いので初めからそう決めた場合でも毎回知らせる。
        # ボタンが増減すると黙って隣を押すため、ログに残っていないと後で追えない
        if how == MATCH_PATH:
            self._emit(
                "warn",
                f"  {describe_element(ref)} を構造上の位置で見つけました。"
                "ボタンの増減があると別のものを操作します。",
            )
            return
        if how == MATCH_AUTO_ID or not ref.auto_id:
            return
        self._emit(
            "warn",
            f"  {describe_element(ref)} を{how}で見つけました。"
            "対象アプリが更新された可能性があります。",
        )

    def _probe(self, ref: ElementRef, step: Step) -> None:
        """確認実行用。操作せず、見つかるかだけ確かめる。"""
        _, how = self._resolve(ref, min(self._timeout(step), 10))
        self._warn_if_fallback(ref, how)

    # ------------------------------------------------------------------
    # 待ち
    # ------------------------------------------------------------------
    def _wait_for(self, step: Step) -> None:
        """手順に記録された「終わった合図」を待つ。

        sleep で書くと実行環境や当日の負荷で必ずいつか足りなくなるので、
        条件を手順の一部として持たせている。
        """
        wait = step.params.get("wait_for") or {}
        kind = wait.get("kind", "none")
        if kind in ("", "none") or self._dry_run:
            return

        timeout = self._timeout(step)

        if kind == "window":
            title = expand(str(wait.get("title", "")), self._variables)
            self._wait_window(title, timeout)
            return

        if kind == "file_exists":
            path = self._resolve_path(str(wait.get("path", "")))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if path.is_file():
                    return
                time.sleep(_POLL_SEC)
            raise AutomationError(f"{path} ができませんでした（{timeout} 秒待機）。")

        ref = ElementRef.from_dict(wait["target"]) if wait.get("target") else None
        if ref is None:
            return

        deadline = time.monotonic() + timeout
        detail = ""
        while time.monotonic() < deadline:
            try:
                control, _ = self._resolve(ref, timeout=1)
            except AutomationError:
                time.sleep(_POLL_SEC)
                continue

            if kind == "element":
                return
            if kind == "element_enabled":
                if control.is_enabled():
                    return
                detail = "まだ押せる状態になりません"
            elif kind == "text_contains":
                expected = expand(str(wait.get("value", "")), self._variables)
                detail = self._text_of(control)
                if expected in detail:
                    return
            time.sleep(_POLL_SEC)

        raise AutomationError(
            f"{timeout} 秒待ちましたが、終わった合図がありませんでした"
            f"（{describe_element(ref)}）。"
            + (f"\n現在の状態: 「{detail}」" if detail else "")
            + "\n処理に時間がかかっているなら、待ち時間を延ばしてください。"
        )

    def _wait_window(self, title: str, timeout: float) -> Any:
        """指定した題名のウィンドウが出るまで待つ。

        pywinauto の UIA バックエンドは、デスクトップ配下を走査するときに
        共通ダイアログ（「名前を付けて保存」など）を取りこぼす。
        HWND を列挙して handle から作る winfind を先に使う。
        """
        return winfind.spec(self._wait_window_handle(title, timeout))

    def _wait_window_handle(self, title: str, timeout: float) -> int:
        """指定した題名のウィンドウが出るまで待ち、その HWND を返す。"""
        if not title:
            raise AutomationError("待つウィンドウの題名が指定されていません。")

        pid = getattr(self._app, "process", None) if self._app is not None else None
        scopes: list[int | None] = ([pid] if pid is not None else []) + [None]
        deadline = time.monotonic() + timeout

        while True:
            for scope in scopes:
                handles = winfind.find_windows(title=title, pid=scope)
                if handles:
                    return handles[0]

            if time.monotonic() >= deadline:
                break
            time.sleep(_POLL_SEC)

        raise AutomationError(
            f"ウィンドウ「{title}」が {timeout} 秒以内に出ませんでした。"
        )

    def _dismiss_overwrite_prompt(self) -> None:
        """「上書きしますか？」が出ていれば「はい」を押す。"""
        pid = getattr(self._app, "process", None) if self._app is not None else None
        for title in ("名前を付けて保存", "上書きの確認", "Confirm Save As"):
            dialog = winfind.find_dialog(title, pid)
            if dialog is None:
                continue
            # IDYES = 6
            if self._click_dialog_button(dialog, ("6",), ("はい", "Yes")):
                return

    def _click_dialog_button(
        self, dialog: Any, auto_ids: tuple[str, ...], names: tuple[str, ...]
    ) -> bool:
        """ダイアログのボタンを押す。

        AutomationId を先に試す。共通ダイアログのボタンは日本語 Windows でも
        名前が Save / Cancel のままなので、名前で探すと見つからないため。
        （IDOK = 1、IDCANCEL = 2、IDYES = 6）
        """
        candidates = [
            dialog.child_window(auto_id=auto_id, control_type="Button")
            for auto_id in auto_ids
        ]
        for name in names:
            candidates.append(dialog.child_window(title=name, control_type="Button"))
            candidates.append(
                dialog.child_window(title_re=f".*{name}.*", control_type="Button")
            )

        for spec in candidates:
            try:
                if spec.exists(timeout=1):
                    spec.wrapper_object().click_input()
                    return True
            except Exception:  # noqa: BLE001 - 次の候補を試す
                continue
        return False

    # ------------------------------------------------------------------
    # 補助
    # ------------------------------------------------------------------
    def _window_for(self, ref: ElementRef, timeout: float) -> Any:
        """要素が属するウィンドウを得る。

        起動したプロセスの中だけを探すと、ランチャー型のアプリ（起動用の
        exe が別プロセスの本体を立ち上げるもの）で空振りする。
        プロセス内 → デスクトップ全体 → HWND 列挙 →
        起動したプロセスの最前面、の順に試す。
        """
        wait = min(timeout, 5)

        if ref.window_title:
            for source in self._window_sources():
                window = source.window(title=ref.window_title)
                if window.exists(timeout=wait):
                    return window

            # UIA の走査から漏れるウィンドウ（共通ダイアログなど）への保険
            pid = getattr(self._app, "process", None) if self._app else None
            window = winfind.find_window(ref.window_title, pid)
            if window is not None:
                return window

        if self._app is not None:
            try:
                return self._app.top_window()
            except Exception as exc:  # noqa: BLE001
                raise AutomationError(
                    f"対象アプリのウィンドウが見つかりません: {exc}"
                ) from exc

        raise AutomationError(
            f"ウィンドウ「{ref.window_title}」が見つかりません。"
            "アプリを起動する手順が先にあるか確認してください。"
        )

    def _window_sources(self) -> list[Any]:
        """ウィンドウを探す対象。起動したプロセスを優先し、無ければ全体。"""
        sources: list[Any] = []
        if self._app is not None:
            sources.append(self._app)
        sources.append(Desktop(backend="uia"))
        return sources

    def _require_element(self, step: Step) -> ElementRef:
        ref = step.element()
        if ref is None:
            raise AutomationError("対象の要素が指定されていません。")
        return ref

    def _timeout(self, step: Step, default: int = 15) -> float:
        try:
            return float(step.params.get("timeout") or default)
        except (TypeError, ValueError):
            return float(default)

    def _resolve_path(self, raw: str) -> Path:
        """差し込みを展開し、相対パスは作業フォルダ基準にする。"""
        expanded = expand(raw, self._variables)
        path = Path(expanded)
        return path if path.is_absolute() else self._work_dir / path

    @staticmethod
    def _stamp(when: datetime) -> str:
        """ログに出す日時。0 時ちょうどなら日付だけにする。"""
        if (when.hour, when.minute, when.second) == (0, 0, 0):
            return when.strftime("%Y-%m-%d")
        return when.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _text_of(control: UIAWrapper) -> str:
        """表示中の文字を取り出す。"""
        try:
            text = control.window_text()
        except Exception:  # noqa: BLE001
            return ""
        return text or ""

    @staticmethod
    def _value_of(control: UIAWrapper) -> str:
        """表示名を持たない欄の中身を、古い形式の値から読む。

        入力欄やラベルは window_text() で読めるが、独自のコントロールだと
        空を返しつつ LegacyIAccessible には値が入っていることがある。
        """
        try:
            value = control.legacy_properties().get("Value", "")
        except Exception:  # noqa: BLE001 - 対応していない要素もある
            return ""
        return str(value or "")

    def _capture_failure(self, index: int) -> Path | None:
        """失敗した瞬間の画面を残す。

        月 1 回しか動かさないので、失敗したときに「どこで止まったか」が
        あとから分かる形で残っていないと調査に時間がかかる。
        """
        if self._dry_run:
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self._log_dir / f"{self._scenario.name}_{stamp}_手順{index}.png"
        try:
            _capture_screen(path)
        except OSError:  # 記録に失敗しても実行結果は変えない
            return None

        self._emit("info", f"  失敗時の画面を保存しました: {path}")
        return path

    def _emit(self, kind: str, text: str) -> None:
        self._on_event(kind, text)
