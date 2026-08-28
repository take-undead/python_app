"""手順を実行する（Tkinter に依存しない）。

対象アプリを起動し、記録された識別子で要素を探して操作する。
座標は使わない。

要素の照合は 3 段構えにしてある。アプリが更新されて AutomationId が
変わっても、名前や構造上の位置で見つかれば動き続ける。どの段で
見つかったかは実行ログに残し、上位の段が外れたら警告を出す。
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pywinauto import Application, Desktop
from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto.uia_element_info import UIAElementInfo

from logic import merger, winfind
from logic.actions import Scenario, Step, build_variables, expand
from logic.picker import ElementRef, describe as describe_element

# 要素が見つかるまでの待ち・確認の刻み
_POLL_SEC = 0.4

# 照合に使った手段。上ほど信頼できる
MATCH_AUTO_ID = "AutomationId"
MATCH_NAME = "名前"
MATCH_PATH = "構造上の位置"


class AutomationError(Exception):
    """手順の実行に失敗した。"""


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

        self._work_dir = scenario.resolved_work_dir(base_dir)
        self._variables = build_variables(self._work_dir, today)
        self._log_dir = base_dir / "logs" / datetime.now().strftime("%Y-%m")

        self._app: Application | None = None
        self._cancelled = False

    # ------------------------------------------------------------------
    # 実行
    # ------------------------------------------------------------------
    def cancel(self) -> None:
        """実行中の手順が終わり次第、止める。"""
        self._cancelled = True

    def run(self, upto: int | None = None) -> RunReport:
        """手順を順に実行する。

        upto を指定すると、そこまでで止める（「ここまで試し実行」用）。
        """
        report = RunReport(dry_run=self._dry_run)
        self._work_dir.mkdir(parents=True, exist_ok=True)

        steps = self._scenario.steps[: upto if upto is not None else None]
        mode = "確認実行" if self._dry_run else "実行"
        self._emit("info", f"{mode}を開始します（{len(steps)} 手順）")

        for index, step in enumerate(steps, start=1):
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
            "save_dialog": self._do_save_dialog,
            "assert_text": self._do_assert_text,
            "assert_file": self._do_assert_file,
            "merge_csv": self._do_merge_csv,
            "close_app": self._do_close_app,
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
        if self._dry_run:
            return f"起動できる状態です（{target.name}）"

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
            control.click_input()
        except Exception as exc:  # noqa: BLE001
            # マウスが届かない配置のときは UIA のパターンで押す
            try:
                control.invoke()
            except Exception:  # noqa: BLE001
                raise AutomationError(
                    f"{describe_element(ref)} を押せませんでした: {exc}"
                ) from exc
        return f"押しました（{how} で照合）"

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

    def _do_merge_csv(self, step: Step) -> str:
        pattern = expand(str(step.params.get("pattern", "")), self._variables)
        output = self._resolve_path(str(step.params.get("output", "")))
        add_source = bool(step.params.get("add_source", True))
        min_rows = int(step.params.get("min_rows", 2) or 0)

        try:
            sources = merger.find_sources(self._work_dir, pattern)
        except merger.MergeError as exc:
            if self._dry_run:
                return f"（確認実行のため対象なし: {exc}）"
            raise AutomationError(str(exc)) from exc

        if self._dry_run:
            return f"{len(sources)} 件をまとめる予定です"

        try:
            result = merger.merge(
                sources, output, add_source=add_source, min_rows=min_rows
            )
        except merger.MergeError as exc:
            raise AutomationError(str(exc)) from exc

        if result.added_columns:
            self._emit(
                "warn",
                "  ファイルごとに列が違います。後から足した列: "
                + "、".join(result.added_columns),
            )
        return (
            f"{result.source_count} 件を {result.row_count} 行にまとめました"
            f"（{result.output.name}）"
        )

    def _do_close_app(self, step: Step) -> str:
        if self._dry_run or self._app is None:
            return "閉じる対象はありません"
        try:
            self._app.kill(soft=True)
        except Exception:  # noqa: BLE001 - 既に閉じていることがある
            pass
        self._app = None
        return "閉じました"

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
    def _text_of(control: UIAWrapper) -> str:
        """表示中の文字を取り出す。"""
        try:
            text = control.window_text()
        except Exception:  # noqa: BLE001
            return ""
        return text or ""

    def _capture_failure(self, index: int) -> Path | None:
        """失敗した瞬間の画面を残す。

        月 1 回しか動かさないので、失敗したときに「どこで止まったか」が
        あとから分かる形で残っていないと調査に時間がかかる。
        """
        if self._dry_run:
            return None
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self._log_dir / f"{self._scenario.name}_{stamp}_手順{index}.png"
            Desktop(backend="uia").capture_as_image().save(path)
            self._emit("info", f"  失敗時の画面を保存しました: {path}")
            return path
        except Exception:  # noqa: BLE001 - 記録に失敗しても実行結果は変えない
            return None

    def _emit(self, kind: str, text: str) -> None:
        self._on_event(kind, text)
