"""Win RPA のエントリポイント。

実行方法（リポジトリ直下から）:
    python apps/win_rpa/main.py                      画面を開く
    python apps/win_rpa/main.py --run 売上集計         無人実行（スケジューラ用）
    python apps/win_rpa/main.py --run 売上集計 --dry-run  操作せず確認だけ
"""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from datetime import datetime
from tkinter import ttk

from logic import storage
from logic.actions import ScenarioError
from logic.runner import Runner


def run_headless(name: str, *, dry_run: bool) -> int:
    """画面を出さずにシナリオを実行する。

    タスクスケジューラから pythonw.exe で呼ばれると標準出力が捨てられるので、
    経過はログファイルに残す。月 1 回しか動かないため、あとから
    「何が起きたか」を追えることが実行そのものと同じくらい重要になる。
    """
    try:
        scenario = storage.load(name)
    except ScenarioError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2

    log_dir = storage.app_dir() / "logs" / datetime.now().strftime("%Y-%m")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode = "確認" if dry_run else "実行"
    log_path = log_dir / f"{storage.safe_name(name)}_{stamp}_{mode}.log"

    with log_path.open("w", encoding="utf-8") as log:

        def emit(kind: str, text: str) -> None:
            line = f"{datetime.now():%H:%M:%S} [{kind}] {text}"
            print(line)
            log.write(line + "\n")
            log.flush()

        runner = Runner(
            scenario, storage.work_root(), on_event=emit, dry_run=dry_run
        )
        report = runner.run()

    print(f"ログ: {log_path}")
    return 0 if report.ok else 1


def _report_startup_failure(exc: BaseException) -> None:
    """起動時の例外を必ず人の目に見える形にする。

    ショートカットや exe から pythonw.exe で起動すると標準エラーが
    どこにも出ず、ダブルクリックしても何も起きない状態になる。
    ログに残したうえでダイアログに出す。
    """
    import traceback

    log_dir = storage.app_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "起動エラー.log"

    detail = "".join(traceback.format_exception(exc))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n===== {stamp} =====\n{detail}")

    print(detail, file=sys.stderr)
    try:
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "起動できませんでした",
            f"{type(exc).__name__}: {exc}\n\n詳しい内容: {log_path}",
        )
        root.destroy()
    except Exception:  # noqa: BLE001 - Tk 自体が使えないときは諦める
        pass


def run_gui() -> None:
    from ui.main_window import MainWindow

    root = tk.Tk()
    root.title("Win RPA")
    root.geometry("1100x760")
    root.minsize(900, 600)

    # 日本語が崩れないよう、Windows 標準の日本語フォントを明示する
    style = ttk.Style(root)
    style.configure(".", font=("Meiryo UI", 10))

    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    window = MainWindow(root)
    window.grid(row=0, column=0, sticky="nsew")

    def on_close() -> None:
        # 未保存の編集や実行中の手順があれば、ここで引き止める
        if not window.confirm_close():
            return
        window.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Windows アプリの自動操作")
    parser.add_argument("--run", metavar="シナリオ名", help="無人で実行する")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="操作せず、要素が見つかるかだけ確認する",
    )
    args = parser.parse_args()

    if args.run:
        raise SystemExit(run_headless(args.run, dry_run=args.dry_run))

    try:
        run_gui()
    except Exception as exc:  # noqa: BLE001 - 起動失敗を握り潰さない
        _report_startup_failure(exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
