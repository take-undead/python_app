"""画面から読み取った値を、CSV に 1 行ずつ足していく（Tkinter に依存しない）。

「CSV をまとめる」（merger.py）が、対象アプリが吐いた複数のファイルを
1 本にする役なのに対し、こちらは**そのつど画面に出た 1 つの値を
同じファイルの末尾に足していく**役。シナリオを動かすたびに 1 行増え、
月をまたいでも 1 本の連続データとして残る。

書き足すだけで、既にある行は読みも消しもしない。途中で失敗しても、
それまでに記録した行はそのまま残る。
"""

from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path

# 記録する列。並びは固定。先頭を日時にしてあるので、出来たファイルは
# そのまま「CSV をまとめる」の並べ替え（先頭 4 列から日時列を探す）に乗る
COLUMNS: tuple[str, ...] = ("日時", "シナリオ", "項目", "値", "元の表示")

# 既にあるファイルを読むときの文字コード。自分で書くときは utf-8-sig
# （Excel で開いても日本語が化けないため）
_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "cp932", "utf-8")

# 数値を取り出すときに、半角に直す文字。業務アプリは全角で出すことがある
_TO_HALF = str.maketrans(
    "０１２３４５６７８９．，－＋％　", "0123456789.,-+% "
)

# マイナスの書き方。会計系の表示では △ や ▲ で負を表すことがある
_MINUS_MARKS: tuple[str, ...] = ("△", "▲", "−", "ー", "―")

_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


class RecordError(Exception):
    """記録に失敗した。"""


def to_number(text: str) -> str:
    """表示されている文字から数値の部分だけを取り出す。

    「1,234 件」「¥12,345」「12.5%」のように、単位や記号が付いた
    まま出ている表示から数値だけを拾う。取り出せなければ空文字を返す
    （呼び出し側が失敗として扱えるようにする。0 で代用しない）。

    負の表し方は 3 通りとも受ける: -123 / △123 / (123)。
    """
    raw = (text or "").translate(_TO_HALF)
    raw = " ".join(raw.split())
    if not raw:
        return ""

    # 括弧書きの負数。数字を拾う前に判定する（括弧は数値の一部ではない）
    negative = raw.startswith("(") and raw.endswith(")")
    if negative:
        raw = raw[1:-1].strip()
    for mark in _MINUS_MARKS:
        if raw.startswith(mark):
            negative = True
            raw = raw[len(mark):].strip()
            break

    found = _NUMBER.search(raw)
    if found is None:
        return ""

    value = found.group().replace(",", "")
    if value.startswith("+"):
        value = value[1:]
    if value.startswith("-"):
        negative = not negative
        value = value[1:]

    # 「12.」のような末尾の小数点は落とす（そのままだと数値として読めない）
    value = value.rstrip(".")
    if not value:
        return ""
    return ("-" if negative and value != "0" else "") + value


def _detect_encoding(path: Path) -> str:
    """既にあるファイルの文字コードを、見出し行が読めるかで決める。"""
    for encoding in _ENCODINGS:
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                handle.readline()
            return encoding
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            raise RecordError(f"{path.name} を読めませんでした: {exc}") from exc

    raise RecordError(
        f"{path.name} の文字コードを判別できませんでした"
        f"（{', '.join(_ENCODINGS)} を試行）。"
    )


def _header_of(path: Path, encoding: str) -> list[str]:
    """既にあるファイルの見出しを読む。"""
    try:
        with path.open("r", encoding=encoding, newline="") as handle:
            row = next(csv.reader(handle), [])
    except OSError as exc:
        raise RecordError(f"{path.name} を読めませんでした: {exc}") from exc
    return [name.strip() for name in row]


def row_count(path: Path) -> int:
    """見出しを除いた行数。ファイルが無ければ 0。

    画面のプレビューと実行ログで「今何行あるか」を出すために使う。
    読めないファイルでも例外にせず 0 を返す（組み立て中に手が止まらない
    ようにするため。書き込みのときは append が理由を出して止める）。
    """
    if not path.is_file():
        return 0
    try:
        encoding = _detect_encoding(path)
        with path.open("r", encoding=encoding, newline="") as handle:
            return max(0, sum(1 for row in csv.reader(handle) if any(row)) - 1)
    except (RecordError, OSError):
        return 0


def _ends_with_newline(path: Path) -> bool:
    """最後の行が改行で終わっているか。

    終わっていないファイルにそのまま足すと、最後の行と足した行が
    1 行につながる。手で編集したファイルで起きる。
    """
    try:
        size = path.stat().st_size
        if size == 0:
            return True
        with path.open("rb") as handle:
            handle.seek(-1, 2)
            return handle.read(1) in (b"\n", b"\r")
    except OSError as exc:
        raise RecordError(f"{path.name} を読めませんでした: {exc}") from exc


def append(
    path: Path,
    *,
    scenario: str,
    label: str,
    value: str,
    raw: str = "",
    when: datetime | None = None,
) -> int:
    """記録用の CSV に 1 行足し、足したあとの行数を返す。

    ファイルが無ければ見出しを付けて作る。既にあるときは、その見出しの
    並びに合わせて書く（人が列を並べ替えていても壊さない）。

    **記録に必要な列が無いファイルには書かない。** 名前がたまたま同じ
    別の CSV（対象アプリの出力や「CSV をまとめる」の結果）を指していた
    場合に、列のずれた行を足して壊してしまうため。
    """
    stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    values = {
        "日時": stamp,
        "シナリオ": scenario,
        "項目": label,
        "値": value,
        "元の表示": raw,
    }

    exists = path.is_file() and path.stat().st_size > 0
    if exists:
        encoding = _detect_encoding(path)
        columns = _header_of(path, encoding)
        missing = [name for name in COLUMNS if name not in columns]
        if missing:
            raise RecordError(
                f"{path.name} は記録用のファイルではありません"
                f"（「{'」「'.join(missing)}」の列がありません）。\n"
                "別の名前にするか、そのファイルを移動してください。"
            )
        newline_needed = not _ends_with_newline(path)
    else:
        encoding = "utf-8-sig"
        columns = list(COLUMNS)
        newline_needed = False

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding=encoding, newline="") as handle:
            if newline_needed:
                handle.write("\r\n")
            writer = csv.DictWriter(handle, fieldnames=columns, restval="")
            if not exists:
                writer.writeheader()
            writer.writerow({key: values.get(key, "") for key in columns})
    except OSError as exc:
        raise RecordError(f"{path} に書けませんでした: {exc}") from exc

    return row_count(path)
