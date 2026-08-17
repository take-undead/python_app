"""新しいアプリの雛形を生成する。

CLAUDE.md の規約（ui/ と logic/ の分離、grid レイアウト、after() による定期処理、
別スレッドの結果を queue 経由で受け取る形）に沿った状態から始められるようにする。

実行方法（リポジトリ直下から）:
    python tools/new_app.py memo
    python tools/new_app.py memo --title "メモ帳"

生成・更新するもの:
    apps/<アプリ名>/main.py, ui/main_window.py, ui/__init__.py, logic/__init__.py, README.md
    .vscode/settings.json  の python.analysis.extraPaths
    .vscode/launch.json    のデバッグ構成
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = ROOT / "apps"
VSCODE_DIR = ROOT / ".vscode"

# アプリ名は英小文字とアンダースコアのみ（python -m でも扱えるようにするため）
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# 雛形内の置換トークン
_APP = "__APP__"
_TITLE = "__TITLE__"


MAIN_PY = '''"""__TITLE__ のエントリポイント。

実行方法（リポジトリ直下から）:
    python apps/__APP__/main.py
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ui.main_window import MainWindow


def main() -> None:
    root = tk.Tk()
    root.title("__TITLE__")
    root.geometry("800x600")
    root.minsize(480, 360)

    # 日本語が崩れないよう、Windows 標準の日本語フォントを明示する
    style = ttk.Style(root)
    style.configure(".", font=("Meiryo UI", 10))

    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    window = MainWindow(root)
    window.grid(row=0, column=0, sticky="nsew")

    def on_close() -> None:
        window.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
'''


MAIN_WINDOW_PY = '''"""メイン画面。

時間のかかる処理を別スレッドに逃がし、結果を queue 経由で受け取る形にしてある。
同期処理しか無いアプリでは _run / _tick まわりは削除してよい。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable

# 定期処理の間隔（ミリ秒）。映像を扱うなら 33（約 30fps）にする
_TICK_MS = 100


class MainWindow(ttk.Frame):
    """__TITLE__ のメイン画面。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=8)

        self._queue: queue.Queue[tuple[Callable[[Any], None], Any]] = queue.Queue()
        self._busy = False
        self._tick_id: str | None = None

        self._status_var = tk.StringVar(value="準備完了")

        self._build_widgets()
        self._tick()

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        control = ttk.Frame(self)
        control.grid(row=0, column=0, sticky="ew")

        self._run_button = ttk.Button(
            control, text="実行", command=self._on_run, width=10
        )
        self._run_button.grid(row=0, column=0)

        # 右端に余白を寄せて、操作部を左詰めにする
        control.columnconfigure(1, weight=1)

        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew", pady=8)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        # ここに画面本体を組む
        ttk.Label(body, text="ここに内容を配置する", anchor="center").grid(
            row=0, column=0, sticky="nsew"
        )

        status = ttk.Label(self, textvariable=self._status_var, anchor="w")
        status.grid(row=2, column=0, sticky="ew")

    # ------------------------------------------------------------------
    # 非同期処理（通信やファイル入出力は必ず別スレッドで行う）
    # ------------------------------------------------------------------
    def _run(
        self,
        task: Callable[[], Any],
        on_success: Callable[[Any], None],
        busy_message: str,
    ) -> None:
        if self._busy:
            return

        self._set_busy(True)
        self._status_var.set(busy_message)

        def worker() -> None:
            try:
                result = task()
            except Exception as exc:  # logic 側が投げる専用の例外型に絞ること
                self._queue.put((self._on_task_error, exc))
            else:
                self._queue.put((on_success, result))

        threading.Thread(target=worker, name="__APP__-task", daemon=True).start()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in (self._run_button,):
            button.configure(state=state)

    def _on_task_error(self, exc: Exception) -> None:
        self._status_var.set("エラーが発生しました。")
        messagebox.showerror("エラー", str(exc), parent=self)

    def _drain_queue(self) -> None:
        while True:
            try:
                callback, result = self._queue.get_nowait()
            except queue.Empty:
                return
            self._set_busy(False)
            callback(result)

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def _on_run(self) -> None:
        self._run(
            lambda: "完了",
            lambda result: self._status_var.set(str(result)),
            "実行しています...",
        )

    # ------------------------------------------------------------------
    # 定期処理
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        self._drain_queue()
        self._tick_id = self.after(_TICK_MS, self._tick)

    # ------------------------------------------------------------------
    # 後始末
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """ウィンドウを閉じるときに呼ぶ。定期処理とスレッドを確実に止める。"""
        if self._tick_id is not None:
            self.after_cancel(self._tick_id)
            self._tick_id = None
'''


README_MD = """# __APP__

（このアプリが何をするかを書く）

## 実行

```powershell
python apps/__APP__/main.py
```

## 構成

```
main.py                エントリポイント
ui/main_window.py      メイン画面
logic/                 業務ロジック（Tkinter を import しない）
```
"""


def _render(template: str, app: str, title: str) -> str:
    return template.replace(_TITLE, title).replace(_APP, app)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"作成: {path.relative_to(ROOT)}")


def create_app(app: str, title: str) -> Path:
    """アプリの雛形一式を生成し、アプリフォルダのパスを返す。"""
    app_dir = APPS_DIR / app

    _write(app_dir / "main.py", _render(MAIN_PY, app, title))
    _write(app_dir / "ui" / "__init__.py", f'"""{title} の画面モジュール群。"""\n')
    _write(app_dir / "ui" / "main_window.py", _render(MAIN_WINDOW_PY, app, title))
    _write(
        app_dir / "logic" / "__init__.py",
        f'"""{title} の業務ロジック（Tkinter に依存しない）。"""\n',
    )
    _write(app_dir / "README.md", _render(README_MD, app, title))

    return app_dir


def update_vscode_settings(app: str) -> None:
    """python.analysis.extraPaths にアプリのパスを追加する。"""
    path = VSCODE_DIR / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))

    paths = data.setdefault("python.analysis.extraPaths", [])
    entry = f"apps/{app}"
    if entry in paths:
        print(f"変更なし: {path.relative_to(ROOT)}（既に登録済み）")
        return

    paths.append(entry)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"更新: {path.relative_to(ROOT)}")


def update_launch_json(app: str) -> None:
    """デバッグ構成を追加する（「現在のファイル」構成の手前に入れる）。"""
    path = VSCODE_DIR / "launch.json"
    data = json.loads(path.read_text(encoding="utf-8"))

    configurations = data.setdefault("configurations", [])
    if any(config.get("name") == app for config in configurations):
        print(f"変更なし: {path.relative_to(ROOT)}（既に登録済み）")
        return

    entry = {
        "name": app,
        "type": "debugpy",
        "request": "launch",
        "program": "${workspaceFolder}/apps/" + app + "/main.py",
        "cwd": "${workspaceFolder}",
        "console": "integratedTerminal",
        "justMyCode": True,
    }

    # 汎用構成（${file}）は末尾に置いたままにする
    index = next(
        (
            i
            for i, config in enumerate(configurations)
            if config.get("program") == "${file}"
        ),
        len(configurations),
    )
    configurations.insert(index, entry)

    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"更新: {path.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="新しいアプリの雛形を生成する")
    parser.add_argument("app", help="アプリ名（英小文字とアンダースコアのみ）")
    parser.add_argument(
        "--title", help="ウィンドウタイトル（省略時はアプリ名をそのまま使う）"
    )
    args = parser.parse_args()

    app = args.app
    if not _NAME_PATTERN.match(app):
        raise SystemExit(
            f"アプリ名 '{app}' は使えません。英小文字で始まり、英小文字・数字・"
            "アンダースコアのみで構成してください。"
        )
    if (APPS_DIR / app).exists():
        raise SystemExit(f"apps/{app} は既に存在します。")

    title = args.title or app

    create_app(app, title)
    update_vscode_settings(app)
    update_launch_json(app)

    print(
        f"\n[完了] 次の手順:\n"
        f"    1. python apps/{app}/main.py で起動を確認する\n"
        f"    2. logic/ に業務ロジックを書く（Tkinter を import しない）\n"
        f"    3. ui/main_window.py に画面を組む\n"
        f"    4. CLAUDE.md の「既存のアプリ」表に 1 行追加する"
    )


if __name__ == "__main__":
    main()
