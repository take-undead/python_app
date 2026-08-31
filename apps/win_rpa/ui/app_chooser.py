"""起動するアプリを、ショートカット一覧から選ぶ小窓。

exe のフルパスを人に入力させない。ショートカットには起動引数と作業フォルダも
入っているので、exe を直接指定するより起動が安定する。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from typing import Any, Callable

from logic.launch import LaunchError, resolve_launch
from logic.shortcuts import Shortcut, ShortcutError, list_shortcuts

_TICK_MS = 100


def shortcut_to_params(shortcut: Shortcut) -> dict[str, Any]:
    """手順に保存する形にする。"""
    return {
        "name": shortcut.name,
        "target": str(shortcut.target),
        "args": shortcut.args,
        "work_dir": str(shortcut.work_dir) if shortcut.work_dir else "",
        "lnk": str(shortcut.lnk),
    }


class AppChooser(tk.Toplevel):
    """ショートカット一覧からアプリを選ぶ。"""

    def __init__(
        self, master: tk.Misc, on_chosen: Callable[[dict[str, Any]], None]
    ) -> None:
        super().__init__(master)
        self._on_chosen = on_chosen
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._tick_id: str | None = None
        self._all: list[Shortcut] = []
        self._shown: list[Shortcut] = []

        self.title("起動するアプリを選ぶ")
        self.geometry("560x460")
        self.minsize(460, 360)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._status_var = tk.StringVar(value="ショートカットを探しています...")
        self._filter_var = tk.StringVar()
        self._detail_var = tk.StringVar(value="")

        self._build_widgets()
        self._tick()
        self._load()

    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self, padding=(8, 8, 8, 0))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="絞り込み").grid(row=0, column=0, padx=(0, 6))
        entry = ttk.Entry(top, textvariable=self._filter_var)
        entry.grid(row=0, column=1, sticky="ew")
        entry.focus_set()
        self._filter_var.trace_add("write", lambda *_: self._apply_filter())

        body = ttk.Frame(self, padding=8)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        self._listbox = tk.Listbox(body, font=("Meiryo UI", 10), activestyle="none")
        self._listbox.grid(row=0, column=0, sticky="nsew")
        self._listbox.bind("<<ListboxSelect>>", lambda _e: self._show_detail())
        self._listbox.bind("<Double-Button-1>", lambda _e: self._choose())

        bar = ttk.Scrollbar(body, orient="vertical", command=self._listbox.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self._listbox.configure(yscrollcommand=bar.set)

        ttk.Label(
            body, textvariable=self._detail_var, foreground="#555555", wraplength=520
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        bottom = ttk.Frame(self, padding=8)
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(1, weight=1)

        ttk.Button(
            bottom, text="ファイルを直接選ぶ...", command=self._choose_file
        ).grid(row=0, column=0)
        ttk.Label(bottom, textvariable=self._status_var).grid(
            row=0, column=1, sticky="w", padx=8
        )
        self._ok_button = ttk.Button(
            bottom, text="決定", command=self._choose, state="disabled", width=10
        )
        self._ok_button.grid(row=0, column=2, padx=(0, 4))
        ttk.Button(bottom, text="取消", command=self._close, width=8).grid(
            row=0, column=3
        )

    # ------------------------------------------------------------------
    def _load(self) -> None:
        """一覧の収集は数秒かかるので別スレッドで行う。"""

        def worker() -> None:
            try:
                items = list_shortcuts()
            except ShortcutError as exc:
                self._queue.put(("error", exc))
            else:
                self._queue.put(("loaded", items))

        threading.Thread(target=worker, name="app-chooser", daemon=True).start()

    def _tick(self) -> None:
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if kind == "loaded":
                self._all = payload
                self._apply_filter()
                self._status_var.set(f"{len(self._all)} 件")
            else:
                self._status_var.set("一覧を取得できませんでした。")
                messagebox.showerror("エラー", str(payload), parent=self)
        self._tick_id = self.after(_TICK_MS, self._tick)

    def _apply_filter(self) -> None:
        keyword = self._filter_var.get().strip().lower()
        self._shown = [
            item
            for item in self._all
            if not keyword
            or keyword in item.name.lower()
            or keyword in str(item.target).lower()
        ]

        self._listbox.delete(0, tk.END)
        for item in self._shown:
            self._listbox.insert(tk.END, f"  {item.name}    〔{item.source}〕")
        self._ok_button.configure(state="disabled")
        self._detail_var.set("")

    def _selected(self) -> Shortcut | None:
        selection = self._listbox.curselection()
        if not selection:
            return None
        return self._shown[selection[0]]

    def _show_detail(self) -> None:
        shortcut = self._selected()
        if shortcut is None:
            return
        detail = f"→ {shortcut.target}"
        if shortcut.args:
            detail += f"\n   引数: {shortcut.args}"
        self._detail_var.set(detail)
        self._ok_button.configure(state="normal")

    def _choose(self) -> None:
        shortcut = self._selected()
        if shortcut is None:
            return
        self._close()
        self._on_chosen(shortcut_to_params(shortcut))

    def _choose_file(self) -> None:
        """一覧に無いアプリのための逃げ道。

        exe とは限らない。ショートカット（拡張子は問わない）や、
        関連付けで別の exe が起動する固有拡張子のファイルも選べる。
        どれを選んでも、ここで実体の exe と引数まで落として保存する。
        """
        path = filedialog.askopenfilename(
            parent=self,
            title="起動するファイルを選ぶ",
            filetypes=[
                ("実行ファイル・ショートカット", "*.exe;*.bat;*.cmd;*.lnk"),
                ("すべて", "*.*"),
            ],
        )
        if not path:
            return

        chosen = Path(path)
        try:
            found = resolve_launch(chosen)
        except LaunchError as exc:
            messagebox.showerror("起動方法が分かりません", str(exc), parent=self)
            return

        # 選んだファイル自体が exe でなければ、何を選んだのかを残しておく。
        # あとで手順を見たとき、exe だけだと元のファイルに辿り着けない
        params = {
            "name": chosen.stem,
            "target": str(found.exe),
            "args": found.args,
            "work_dir": str(found.work_dir) if found.work_dir else "",
            "lnk": "" if found.how == "実行ファイル" else str(chosen),
        }

        if found.how != "実行ファイル":
            messagebox.showinfo(
                "起動方法",
                f"{chosen.name} は{found.how}でした。\n\n"
                f"次のコマンドで起動します。\n{found.command}",
                parent=self,
            )

        self._close()
        self._on_chosen(params)

    def _close(self) -> None:
        if self._tick_id is not None:
            self.after_cancel(self._tick_id)
            self._tick_id = None
        self.destroy()
