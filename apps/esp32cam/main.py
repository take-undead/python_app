"""ESP32-CAM ビューアのエントリポイント。

実行方法（リポジトリ直下から）:
    python apps/esp32cam/main.py
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ui.main_window import MainWindow


def main() -> None:
    root = tk.Tk()
    root.title("ESP32-CAM ビューア（6 台同時表示）")
    root.geometry("1360x820")
    root.minsize(1000, 620)

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
