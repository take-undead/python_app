"""要素ピッカーを単体で動かす（対象アプリの下調べ用）。

自動操作を組み始める前に、対象アプリのボタンやテキスト欄が UIA から
見えるかを確かめるためのもの。見えないアプリ（Tkinter 製など）は
座標クリックに頼るしかなくなるため、先に判別しておく価値が高い。

実行方法（リポジトリ直下から）:
    python apps/win_rpa/pick.py
    python apps/win_rpa/pick.py --out picked.json

使い方:
    1. 調べたいアプリを先に起動しておく
    2. 本スクリプトを起動する
    3. 対象のボタンにマウスを乗せて F8
    4. 終わったら Esc
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from logic.picker import (
    VK_ESCAPE,
    VK_F8,
    ElementRef,
    PickerError,
    capture_at_cursor,
    describe,
    is_identifiable,
    is_key_pressed,
    suggest_action,
)

# キーの押しっぱなしで連続採取しないよう、離すまで待つ間隔
_POLL_SEC = 0.05

_ACTION_LABEL = {
    "click": "押す",
    "check": "チェックする",
    "select": "選ぶ",
    "set_text": "文字を入力する",
    "assert_text": "表示を確認する",
}


def _report(ref: ElementRef, index: int) -> None:
    print(f"\n[{index}] {describe(ref)}")
    print(f"    推定される操作 : {_ACTION_LABEL.get(suggest_action(ref), '押す')}")
    print(f"    ウィンドウ     : {ref.window_title or '(名前なし)'}")
    print(f"    AutomationId   : {ref.auto_id or '(なし)'}")
    print(f"    名前           : {ref.name or '(なし)'}")
    print(f"    ツールチップ   : {ref.help_text or '(なし)'}")
    print(f"    古い形式の名前 : {ref.legacy_name or '(なし)'}")
    print(f"    種類           : {ref.control_type}")
    print(f"    フレームワーク : {ref.framework or '(不明)'}")
    print(f"    構造上の位置   : {' > '.join(f'{t}[{i}]' for t, i in ref.index_path)}")

    if not is_identifiable(ref):
        print("    ⚠ 名前になるものが 1 つもありません。")
        print("      構造上の位置でしか探せません（ボタンが増減すると外れます）。")
        print("      このアプリは UIA に情報を公開していない可能性があります。")


def _wait_release(vk: int) -> None:
    while is_key_pressed(vk):
        time.sleep(_POLL_SEC)


def main() -> None:
    parser = argparse.ArgumentParser(description="カーソル位置の UI 要素を採取する")
    parser.add_argument("--out", type=Path, help="採取結果を JSON で保存する先")
    args = parser.parse_args()

    print("要素ピッカー")
    print("  F8  ... カーソル位置の要素を採取する")
    print("  Esc ... 終了する")
    print("\n調べたいアプリのボタンにマウスを乗せて F8 を押してください。")

    picked: list[ElementRef] = []

    while True:
        if is_key_pressed(VK_ESCAPE):
            _wait_release(VK_ESCAPE)
            break

        if is_key_pressed(VK_F8):
            _wait_release(VK_F8)
            try:
                ref = capture_at_cursor()
            except PickerError as exc:
                print(f"\n  採取できませんでした: {exc}")
            else:
                picked.append(ref)
                _report(ref, len(picked))

        time.sleep(_POLL_SEC)

    print(f"\n終了しました。{len(picked)} 件を採取しました。")

    if args.out and picked:
        args.out.write_text(
            json.dumps(
                [ref.to_dict() for ref in picked], ensure_ascii=False, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"保存: {args.out}")


if __name__ == "__main__":
    main()
