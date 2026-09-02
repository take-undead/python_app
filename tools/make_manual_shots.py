"""マニュアルに載せる画面写真を撮る。

    python tools/make_manual_shots.py win_rpa

`apps/<アプリ名>/images/` に PNG を書き出す。マニュアル（MANUAL.md）から
参照し、`tools/make_manual_pdf.py` が PDF に埋め込む。

**手で撮らずにここから撮る。** 画面の項目名やボタンが変わるたびに撮り直す
必要があり、手で撮ると切り取る範囲も見せる内容も毎回ぶれるため。

写真に写す内容は、この PC の実データではなく**見本のシナリオ**にしてある
（開発機のフォルダやシナリオ名がマニュアルに載らないようにするため）。

実行中は画面に窓が出たり消えたりする。撮っているあいだ PC を触らないこと
（別の窓が手前に出ると、それが写り込む）。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import ttk
from typing import Any

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent

# 画面写真の見た目
_BADGE_R = 15                      # 番号の丸の半径
_MARGIN = 2 * _BADGE_R + 14        # 番号を描くために左右へ足す余白
_INK = (31, 41, 55)                # 丸の色（画面の文字色に合わせる）
_PAPER = (255, 255, 255)


class ShotError(Exception):
    """画面写真を撮れなかった。"""


# ----------------------------------------------------------------------
# 画面の取り込み
# ----------------------------------------------------------------------
def _set_dpi_aware() -> None:
    """取り込む前に DPI を意識させる。

    これをしないと、拡大表示（125% など）の PC で Tkinter が返す座標
    （論理ピクセル）と ImageGrab が撮る画像（物理ピクセル）がずれ、
    切り取る位置が合わなくなる。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _frame_rect(window: tk.Misc) -> tuple[int, int, int, int]:
    """窓の見た目どおりの外枠を返す。

    GetWindowRect は Windows 11 の見えない余白（影の領域）まで含むので、
    DWM に実際の枠を聞く。そのまま撮ると窓の周りに透明な帯が入る。
    """
    hwnd = int(window.wm_frame(), 16)

    rect = wintypes.RECT()
    # DWMWA_EXTENDED_FRAME_BOUNDS = 9
    result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)
    )
    if result != 0:
        ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def _settle(window: tk.Misc, rounds: int = 25) -> None:
    """窓を手前に出し、描き終わるまで待つ。"""
    window.lift()
    for _ in range(rounds):
        window.update()
    window.update_idletasks()


def _pump(window: tk.Misc, seconds: float) -> None:
    """指定した時間、画面を動かし続ける。

    update() を回すだけでは時間が進まない。各画面の定期処理は after() で
    動いていて、別スレッドの結果（スケジュール一覧など）が返るまでに実際の
    時間が要る。待たずに撮ると「確認しています...」のまま写る。
    """
    import time

    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        window.update()
        time.sleep(0.02)


def _grab(window: tk.Misc) -> Image.Image:
    """窓 1 つを画像にする。

    画面を座標で切り取る（ImageGrab）方法は採らない。画面が複数あって
    左や上に並んでいると Windows の座標が負になり、切り取る位置が
    丸ごとずれる。表示倍率でもずれる。

    代わりに PrintWindow で**窓そのものに描かせる**。座標が要らないので
    画面の並びにも倍率にも左右されず、他の窓が重なっていても写り込まない。
    PW_RENDERFULLCONTENT を渡さないと中身が黒く抜ける。
    """
    import win32gui
    import win32ui

    _settle(window)
    hwnd = int(window.wm_frame(), 16)

    outer = win32gui.GetWindowRect(hwnd)
    width, height = outer[2] - outer[0], outer[3] - outer[1]
    if width <= 0 or height <= 0:
        raise ShotError("窓の大きさを取れませんでした。")

    window_dc = win32gui.GetWindowDC(hwnd)
    source = win32ui.CreateDCFromHandle(window_dc)
    target = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(source, width, height)
        target.SelectObject(bitmap)

        if not ctypes.windll.user32.PrintWindow(
            wintypes.HWND(hwnd), target.GetSafeHdc(), 2   # PW_RENDERFULLCONTENT
        ):
            raise ShotError("窓の中身を描き出せませんでした。")

        info = bitmap.GetInfo()
        image = Image.frombuffer(
            "RGB",
            (info["bmWidth"], info["bmHeight"]),
            bitmap.GetBitmapBits(True),
            "raw", "BGRX", 0, 1,
        )
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        target.DeleteDC()
        source.DeleteDC()
        win32gui.ReleaseDC(hwnd, window_dc)

    # Windows 11 の窓は影のぶんだけ外枠が広い。見た目どおりに切り詰める
    left, top, right, bottom = _frame_rect(window)
    return image.crop(
        (left - outer[0], top - outer[1], right - outer[0], bottom - outer[1])
    )


def _region(window: tk.Misc, widget: tk.Misc) -> tuple[int, int, int, int]:
    """窓の画像の中での、ウィジェットの位置を返す。"""
    left, top, _, _ = _frame_rect(window)
    x = widget.winfo_rootx() - left
    y = widget.winfo_rooty() - top
    return x, y, x + widget.winfo_width(), y + widget.winfo_height()


# ----------------------------------------------------------------------
# 番号を振る
# ----------------------------------------------------------------------
def _font(size: int) -> Any:
    for name in ("meiryob.ttc", "meiryo.ttc", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _annotate(
    image: Image.Image, marks: list[tuple[int, tuple[int, int, int, int], str]]
) -> Image.Image:
    """区画に番号を振る。

    番号は画像の**外側**（左右に足した余白）に描く。画面の上に重ねると、
    ちょうど隠したくないラベルやボタンを覆ってしまうため。
    """
    wide = Image.new(
        "RGB", (image.width + _MARGIN * 2, image.height), _PAPER
    )
    wide.paste(image, (_MARGIN, 0))
    draw = ImageDraw.Draw(wide)
    font = _font(int(_BADGE_R * 1.3))

    for number, (x1, y1, x2, y2), side in marks:
        middle = (y1 + y2) // 2
        if side == "left":
            center = (_MARGIN // 2, middle)
            line = (center[0] + _BADGE_R, middle, _MARGIN + x1, middle)
        else:
            center = (wide.width - _MARGIN // 2, middle)
            line = (_MARGIN + x2, middle, center[0] - _BADGE_R, middle)

        draw.line(line, fill=_INK, width=2)
        draw.ellipse(
            [
                center[0] - _BADGE_R, center[1] - _BADGE_R,
                center[0] + _BADGE_R, center[1] + _BADGE_R,
            ],
            fill=_INK,
        )
        draw.text(center, str(number), fill=_PAPER, font=font, anchor="mm")

    return wide


def _save(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, optimize=True)
    print(f"  {path.relative_to(ROOT)}  {image.width}x{image.height}")


# ----------------------------------------------------------------------
# win_rpa
# ----------------------------------------------------------------------
def _demo_scenario(actions: Any) -> Any:
    """写真に写す見本のシナリオを組む。

    保存はしない。この PC のシナリオ一覧を汚さないため、また写真に
    開発機の実データが写らないようにするため。
    """
    Scenario, Step = actions.Scenario, actions.Step

    def element(name: str, kind: str) -> dict[str, Any]:
        return {
            "auto_id": "", "name": name, "control_type": kind,
            "class_name": "", "framework": "WinForm",
            "window_title": "売上管理システム", "window_auto_id": "",
            "index_path": [], "help_text": "", "legacy_name": "",
        }

    def step(action: str, group: str, **params: Any) -> Any:
        made = Step(action=action, group=group)
        for spec_field in made.spec.fields:
            if spec_field.default is not None:
                made.params[spec_field.key] = spec_field.default
        made.params.update(params)
        return made

    collect, export, merge = "集計する", "CSV を出す", "まとめる"

    return Scenario(
        name="売上集計",
        steps=[
            step("launch_app", collect, app={
                "name": "売上管理システム",
                "target": r"C:\Program Files\売上管理\uriage.exe",
                "args": "", "work_dir": "", "lnk": "",
            }),
            step("set_text", collect,
                 target=element("対象年月", "Edit"), value="{prev_yyyymm}"),
            step("select", collect,
                 target=element("集計区分", "ComboBox"), value="月次"),
            step("click", collect, target=element("集計", "Button"),
                 wait_for={"kind": "text_contains", "text": "集計完了"}),
            step("click", export, target=element("CSV出力", "Button"),
                 wait_for={"kind": "window", "title": "名前を付けて保存"}),
            step("save_dialog", export, path="売上_{prev_yyyymm}.csv"),
            step("assert_file", export,
                 path="売上_{prev_yyyymm}.csv", min_rows=2),
            step("record_value", export,
                 target=element("集計結果", "Text"), label="件数",
                 file="{scenario}_記録.csv"),
            step("close_app", export),
            step("make_folder", merge, parent="", name="{yyyymm}"),
            step("copy_files", merge, from_dir="", source="*.csv",
                 dest="{yyyymm}", modified={"kind": "prev_month"}),
            step("merge_csv", merge, output="まとめ_{yyyymm}.csv"),
        ],
    )


def _fill_log(window: Any) -> None:
    """実行ログの区画に、読める内容を入れておく。

    空のまま撮ると、その区画が何を出す場所なのか写真から分からない。
    """
    for kind, text in (
        ("info", "Win RPA 0.9.2（ベータ版）"),
        ("step", "手順 4: ボタン「集計」を押す"),
        ("ok", "  押しました（名前で見つけました）"),
        ("info", "  「集計完了（237 件）」が出るまで待ちます"),
        ("ok", "  1.4 秒で合図を受け取りました"),
        ("step", "手順 12: フォルダの CSV を日時順にまとめて「まとめ_202608.csv」にする"),
        ("info", "  「日時」の古い順に並べ替えました"),
        ("info", "  2026-08-01 10:00:00 〜 2026-08-31 17:00:00 の 93 行"),
        ("ok", "  31 件を 93 行にまとめました"),
    ):
        window._append_log(kind, text)


def shoot_win_rpa(out_dir: Path) -> None:
    """win_rpa の画面写真を一式撮る。"""
    app_dir = ROOT / "apps" / "win_rpa"
    sys.path.insert(0, str(app_dir))

    from logic import actions                      # noqa: PLC0415 - パス設定後
    from ui.about_dialog import AboutDialog        # noqa: PLC0415
    from ui.main_window import MainWindow          # noqa: PLC0415
    from ui.picker_dialog import PickerDialog      # noqa: PLC0415
    from ui.schedule_dialog import ScheduleDialog  # noqa: PLC0415

    root = tk.Tk()
    root.title("Win RPA")
    root.geometry("1180x780")
    ttk.Style(root).configure(".", font=("Meiryo UI", 10))
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    window = MainWindow(root)
    window.grid(row=0, column=0, sticky="nsew")

    # 見本のシナリオを流し込む。保存を通すとこの PC の一覧に残ってしまうので、
    # 画面が持っている値を直接差し替える（写真を撮るためだけの処置）
    demo = _demo_scenario(actions)
    window._scenario = demo
    window._saved_state = demo.to_dict()
    window._scenario_box.configure(values=[demo.name, "在庫棚卸し", "月次請求"])
    window._scenario_var.set(demo.name)
    window._refresh_steps()
    _fill_log(window)
    window._status_var.set("完了しました。")

    # 「ボタンを押す」を選んでおく。設定の区画に何も出ていないと、
    # そこが何をする場所なのか写真から分からない
    window._steps.selection_set("s3")
    window._steps.see("s3")
    for _ in range(10):
        root.update()

    print("win_rpa の画面を撮ります:")

    main = _grab(root)
    _save(main, out_dir / "main.png")

    marks = [
        # ツールバーは帯そのものを指す。中の選択欄を指すと、引き出し線が
        # 左隣の「シナリオ」の文字を横切ってしまう
        (1, _region(root, window._scenario_box.master), "left"),
        (2, _region(root, window._steps), "left"),
        (3, _region(root, window._form), "right"),
        (4, _region(root, window._dry_button.master), "right"),
        (5, _region(root, window._log), "left"),
    ]
    _save(_annotate(main, marks), out_dir / "main_regions.png")

    # 実行ボタンの帯だけを切り出す。4 つの違いを説明する章で使う
    left, top, right, bottom = _region(root, window._dry_button.master)
    _save(main.crop((left - 8, top - 6, right + 8, bottom + 6)),
          out_dir / "run_buttons.png")

    # 手順一覧（グループにまとまった様子）
    left, top, right, bottom = _region(root, window._steps)
    _save(main.crop((left - 4, top - 40, right + 4, bottom + 4)),
          out_dir / "steps.png")

    # 本体は隠さない。小窓は transient なので、親を withdraw すると
    # 一緒に非表示になり、大きさが決まらないまま撮ることになる
    root.attributes("-topmost", False)

    for name, make in (
        ("picker", lambda: PickerDialog(window, lambda _ref: None)),
        ("about", lambda: AboutDialog(window)),
        ("schedule", lambda: ScheduleDialog(window, demo.name)),
    ):
        dialog = make()
        dialog.update_idletasks()
        dialog.deiconify()
        # スケジュールの小窓は schtasks の一覧が返るまで数秒かかる
        _pump(root, 6.0 if name == "schedule" else 0.5)

        if name == "picker":
            # この小窓は 0.2 秒ごとにマウスの下にあるものを出し直す。
            # 撮影時にたまたま乗っていたものが写ると何の写真か分からないので、
            # 定期処理を止めてから、対象アプリのボタンに乗せた表示にする
            # （止めないと F8 / Esc の監視も動いたままになる）
            if dialog._tick_id is not None:
                dialog.after_cancel(dialog._tick_id)
                dialog._tick_id = None
            dialog._peek_var.set("ボタン「集計」")
            dialog.update_idletasks()

        _save(_grab(dialog), out_dir / f"{name}.png")
        dialog.destroy()
        root.update()

    window.shutdown()
    root.destroy()


# ----------------------------------------------------------------------
# webcam
# ----------------------------------------------------------------------
def _color_bars(width: int, height: int) -> Any:
    """説明用の見本映像（カラーバー）を作る。

    **実際のカメラは開かない。** 開発機のカメラを回すと、その場の部屋や
    人がそのままマニュアルに載る。映像が出ている状態とボタンの見た目を
    見せたいだけなので、明らかに見本と分かる絵を流し込む。
    """
    import numpy as np

    colors = (
        (192, 192, 192), (0, 192, 192), (192, 192, 0), (0, 192, 0),
        (192, 0, 192), (0, 0, 192), (192, 0, 0), (32, 32, 32),
    )
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    step = width // len(colors)
    for index, color in enumerate(colors):
        left = index * step
        right = width if index == len(colors) - 1 else left + step
        frame[:, left:right] = color   # OpenCV と同じ BGR の並び
    return frame


def shoot_webcam(out_dir: Path) -> None:
    """webcam の画面写真を撮る。"""
    sys.path.insert(0, str(ROOT / "apps" / "webcam"))
    from ui.main_window import MainWindow   # noqa: PLC0415 - パス設定後

    root = tk.Tk()
    root.title("Web カメラビューア")
    root.geometry("800x640")
    ttk.Style(root).configure(".", font=("Meiryo UI", 10))
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    window = MainWindow(root)
    window.grid(row=0, column=0, sticky="nsew")
    _pump(root, 0.4)

    print("webcam の画面を撮ります:")
    _save(_grab(root), out_dir / "main.png")

    # 表示中の見た目。カメラは開かず、ボタンの状態と見本の映像だけを作る
    window._current_frame = _color_bars(640, 480)
    window._show_frame(window._current_frame)
    window._start_button.configure(state="disabled")
    window._detect_button.configure(state="disabled")
    window._device_combo.configure(state="disabled")
    window._stop_button.configure(state="normal")
    window._snapshot_button.configure(state="normal")
    window._status_var.set("カメラ 0 を表示中です。")
    _pump(root, 0.3)

    _save(_grab(root), out_dir / "running.png")

    window.shutdown()
    root.destroy()


# ----------------------------------------------------------------------
# webpin
# ----------------------------------------------------------------------
def _demo_samples(protocol: Any, seconds: float = 30.0, step: float = 0.2) -> list[Any]:
    """説明用の見本データを作る。

    マイコンをつながずに撮るため。空のグラフだけを載せても、この画面が
    何を出すところなのか伝わらない。
    """
    import math

    Sample = protocol.Sample
    names = ("AIN1(GP36)", "AIN2(GP39)", "AIN3(GP32)", "AIN4(GP33)")
    outputs = {f"DOUT{n}": pin for n, pin in enumerate((17, 2, 15, 13, 12), start=1)}

    samples = []
    start = 1_770_000_000.0          # 見本なので固定の時刻から始める
    count = int(seconds / step)

    for index in range(count):
        moment = index * step
        values: dict[str, float] = {}

        for order, name in enumerate(names):
            wave = math.sin(moment * (0.6 + order * 0.35) + order) * 0.5 + 0.5
            raw = round(wave * 3600 + 200)
            values[f"{name}.raw"] = float(raw)
            values[f"{name}.volt"] = round(raw * 3.3 / 4095, 3)

        values["DIN1(GP37)"] = float(int(moment) % 6 < 3)
        values["DIN2(GP38)"] = float(int(moment) % 10 < 4)
        for order, name in enumerate(outputs):
            # DOUT1 だけを動かし、最後は ON で終わらせる。
            # 全部 OFF のまま撮ると、押した状態の見た目が写らない
            values[name] = float(order == 0 and int(moment) % 8 < 6)

        values["audio.playing"] = float(int(moment) % 12 < 5)
        values["audio.freq"] = 1000.0
        values["audio.volume"] = 200.0

        samples.append(
            Sample(timestamp=start + moment, values=values, outputs=dict(outputs))
        )

    return samples


def shoot_webpin(out_dir: Path) -> None:
    """webpin の画面写真を撮る。"""
    sys.path.insert(0, str(ROOT / "apps" / "webpin"))
    from logic import protocol            # noqa: PLC0415 - パス設定後
    from ui.main_window import MainWindow  # noqa: PLC0415

    root = tk.Tk()
    root.title("webpin - ピンロガー")
    root.geometry("1180x720")
    ttk.Style(root).configure(".", font=("Meiryo UI", 10))
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    window = MainWindow(root)
    window.grid(row=0, column=0, sticky="nsew")
    _pump(root, 0.4)

    print("webpin の画面を撮ります:")
    _save(_grab(root), out_dir / "main.png")

    # 受信したことにして画面を埋める。_poll() が受信時にしていることと
    # 同じ順序で行う（ピン一覧と出力ボタンは、届いて初めて作られるため）
    for sample in _demo_samples(protocol):
        window._store.add(sample)
        window._dout_pins.update(sample.outputs)

    window._add_pin_rows(window._store.pins())
    window._add_dout_buttons(list(window._dout_pins))
    window._sync_dout_states()
    # 出力ボタンは未接続だと押せない見た目になる。接続中の様子を撮りたいので
    # 押せる状態にしておく（_update_dout_buttons() は接続の有無で決めるため使わない）
    for button in window._dout_buttons.values():
        button.configure(state="normal")

    for pin, var in window._pin_value_vars.items():
        value = window._store.latest(pin)
        var.set("-" if value is None else f"{value:g}")
    window._count_var.set(f"受信 {window._store.sample_count} 件")
    window._status_var.set("ws://192.168.1.132/ws に接続しました。")
    window._log_var.set("記録中: log_20260902_114000.csv（150 行）")
    window._redraw_charts()
    _pump(root, 0.5)

    _save(_grab(root), out_dir / "receiving.png")

    # 上の操作の帯と、右側のピン一覧はそれぞれ章で使うので切り出しておく
    main = _grab(root)
    left, top, right, bottom = _region(root, window._connect_button.master)
    _save(main.crop((left - 6, top - 6, right + 6, bottom + 6)),
          out_dir / "control_bar.png")

    left, top, right, bottom = _region(root, window._pin_container.master.master)
    _save(main.crop((left - 4, top - 22, right + 4, bottom + 4)),
          out_dir / "pins.png")

    window.shutdown()
    root.destroy()


# ----------------------------------------------------------------------
# esp32cam
# ----------------------------------------------------------------------
# 6 台のタイルを見分けられるよう、カメラごとに色を変える
_CAMERA_HUES: tuple[tuple[int, int, int], ...] = (
    (38, 70, 110), (36, 96, 84), (96, 62, 40),
    (86, 48, 96), (40, 84, 112), (92, 78, 36),
)


def _demo_scene(number: int, width: int = 640, height: int = 480) -> Image.Image:
    """説明用の見本映像を作る。

    **実際のカメラにはつながない。** 手元に 6 台そろっている保証が無く、
    つながったとしてもその場の風景がそのままマニュアルに載る。台ごとに
    違う映像が並んでいる様子だけを見せたいので、見本と分かる絵にする。
    """
    top = _CAMERA_HUES[(number - 1) % len(_CAMERA_HUES)]
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)

    for y in range(height):
        ratio = y / height
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(int(part * (1.0 - ratio * 0.55)) for part in top),
        )

    draw.text(
        (width // 2, height // 2 - height // 12),
        str(number),
        fill=(255, 255, 255),
        font=_font(int(height * 0.34)),
        anchor="mm",
    )
    draw.text(
        (width // 2, height // 2 + height // 4),
        "見本映像",
        fill=(210, 214, 220),
        font=_font(int(height * 0.075)),
        anchor="mm",
    )
    return image


def _demo_frame(number: int) -> Any:
    """見本映像を、映像受信と同じ形（OpenCV の BGR 配列）にする。"""
    import numpy as np

    return np.array(_demo_scene(number))[:, :, ::-1].copy()


def _demo_photo_names() -> list[str]:
    """SD カード内の写真一覧の見本。ファームウェアが返す形に合わせる。"""
    times = (
        "094512", "101033", "104417", "110255", "113048",
        "114233", "121509", "124002", "131744", "134920",
    )
    return [f"202609/20260902_{stamp}.jpg" for stamp in times]


def shoot_esp32cam(out_dir: Path) -> None:
    """esp32cam の画面写真を撮る。"""
    import io

    sys.path.insert(0, str(ROOT / "apps" / "esp32cam"))
    from ui.main_window import MainWindow      # noqa: PLC0415 - パス設定後
    from ui.photo_preview import PhotoPreview  # noqa: PLC0415

    root = tk.Tk()
    root.title("ESP32-CAM ビューア（6 台同時表示）")
    root.geometry("1180x760")
    ttk.Style(root).configure(".", font=("Meiryo UI", 10))
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    window = MainWindow(root)
    window.grid(row=0, column=0, sticky="nsew")
    _pump(root, 0.5)

    print("esp32cam の画面を撮ります:")
    _save(_grab(root), out_dir / "main_idle.png")

    # 接続したことにして映像を流し込む。タイルは _show_frame で描き、
    # ボタンの状態は接続時と同じにする（_stream は持たせない。
    # 持たせると poll() が中身の無いストリームを触りに行く）
    for tile in window._tiles:
        tile._show_frame(_demo_frame(tile.index + 1))
        tile._connect_button.configure(state="disabled")
        tile._disconnect_button.configure(state="normal")
        tile._host_entry.configure(state="disabled")

    for name in _demo_photo_names():
        window._photo_list.insert(tk.END, name)
    window._photo_list.selection_set(5)
    window._status_var.set("6 / 6 台を表示中です。")
    _pump(root, 0.4)

    main = _grab(root)
    _save(main, out_dir / "main.png")

    # 章ごとに使う部分を切り出す
    left, top, right, bottom = _region(root, window._capture_button.master)
    _save(main.crop((left - 6, top - 6, right + 6, bottom + 6)),
          out_dir / "toolbar.png")

    left, top, right, bottom = _region(root, window._tiles[0])
    _save(main.crop((left - 3, top - 3, right + 3, bottom + 3)),
          out_dir / "tile.png")

    left, top, right, bottom = _region(root, window._photo_list.master.master)
    _save(main.crop((left - 4, top - 4, right + 4, bottom + 4)),
          out_dir / "photos.png")

    # 写真プレビュー。届いた JPEG を渡す形なので、見本の絵を JPEG にして渡す
    buffer = io.BytesIO()
    _demo_scene(1, 1600, 1200).save(buffer, format="JPEG", quality=88)

    names = _demo_photo_names()
    preview = PhotoPreview(
        window, names, 5, buffer.getvalue(),
        Path(out_dir).parent / "downloads",
        on_request=lambda _preview, _index: None,
    )
    _pump(root, 0.6)
    _save(_grab(preview), out_dir / "preview.png")
    preview.destroy()
    root.update()

    window.shutdown()
    root.destroy()


SHOOTERS = {
    "esp32cam": shoot_esp32cam,
    "webcam": shoot_webcam,
    "webpin": shoot_webpin,
    "win_rpa": shoot_win_rpa,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="マニュアル用の画面写真を撮る")
    parser.add_argument("app", choices=sorted(SHOOTERS), help="対象のアプリ")
    args = parser.parse_args()

    _set_dpi_aware()
    out_dir = ROOT / "apps" / args.app / "images"

    try:
        SHOOTERS[args.app](out_dir)
    except ShotError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print(f"\n{out_dir.relative_to(ROOT)} に書き出しました。")


if __name__ == "__main__":
    main()
