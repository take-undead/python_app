"""複数の CSV を 1 つにまとめる（Tkinter に依存しない）。

対象アプリが吐いた CSV を結合する。列の並びがファイルごとに違っても
見出しで対応づける。列が食い違ったまま黙って結合すると、
気づかないうちに壊れた集計表ができるため。

まとめた結果は日時の古い順に並べ替える。日単位のファイルを 1 か月ぶん
まとめて連続データにする用途なので、**時刻まで見て並べる**。
並べ替えの基準にする列は、見出しの先頭付近から自動で見つける
（人に列名を打たせないため）。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Excel が吐く CSV は UTF-8 BOM 付きのことが多いので、まず BOM 付きで試す
_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "cp932", "utf-8")

# 元ファイル名を入れる列。末尾に足すので、日時列の探索には引っかからない
SOURCE_COLUMN = "元ファイル"

# 日時として認める書き方。時刻まで含むものを先に試す。
#
# 日単位のファイルを 1 か月ぶんまとめる用途では、**時刻を落とすと同じ日の中が
# 並ばない**（元ファイルの並び順のまま残る）。日付だけの書き方はその後に試す。
_DATETIME_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y%m%d %H:%M:%S",
    "%Y%m%d %H:%M",
    "%Y%m%d%H%M%S",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日 %H時%M分%S秒",
    "%Y年%m月%d日 %H時%M分",
)

# 日付だけの書き方
_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y%m%d",
    "%Y年%m月%d日",
    "%Y.%m.%d",
    "%Y-%m",
    "%Y/%m",
    "%Y年%m月",
)

# 列名にこれが入っていれば日時列の可能性が高い（小文字で比較する）
_DATE_NAME_HINTS: tuple[str, ...] = (
    "日付", "日時", "年月日", "年月", "月日", "date", "datetime", "day",
)

# 先頭から何列目までを日時列の候補にするか。
# 「データの先頭付近にある日時」で並べるため、後ろの列は見ない
_HEAD_COLUMNS = 4

# 日時らしさの判定に使う行数と、そのうち何割が読めたら日時列と見なすか
_SAMPLE_ROWS = 50
_DATE_RATIO = 0.8


class MergeError(Exception):
    """CSV の結合に失敗した。"""


@dataclass(frozen=True)
class MergeResult:
    """結合の結果。実行ログにそのまま出せる形にしておく。"""

    output: Path
    source_count: int
    row_count: int
    columns: tuple[str, ...]
    added_columns: tuple[str, ...]
    sort_column: str = ""
    unsorted_rows: int = 0
    # 並べ替えたときの最初と最後の日時。1 か月ぶん揃っているかの確認に使う
    first_time: datetime | None = None
    last_time: datetime | None = None


def _read(path: Path) -> tuple[list[str], list[dict[str, str]], str]:
    """CSV を読む。文字コードは順に試す。"""
    last_error: Exception | None = None

    for encoding in _ENCODINGS:
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise MergeError(f"{path.name} に見出し行がありません。")
                rows = [dict(row) for row in reader]
                return list(reader.fieldnames), rows, encoding
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        except OSError as exc:
            raise MergeError(f"{path.name} を読めませんでした: {exc}") from exc

    raise MergeError(
        f"{path.name} の文字コードを判別できませんでした"
        f"（{', '.join(_ENCODINGS)} を試行）: {last_error}"
    )


def find_sources(folder: Path, exclude: Path | None = None) -> list[Path]:
    """フォルダの中の CSV を集める。

    exclude は結果として書き出すファイル。除かないと、2 回目に動かしたとき
    前回の出力を自分自身に取り込んで行が倍になる。
    """
    if not folder.is_dir():
        raise MergeError(f"フォルダがありません: {folder}")

    target = exclude.resolve() if exclude is not None else None
    sources = sorted(
        p
        for p in folder.glob("*.csv")
        if p.is_file() and (target is None or p.resolve() != target)
    )
    if not sources:
        raise MergeError(
            f"{folder} に CSV がありません。"
            "対象アプリが CSV を出せていない可能性があります。"
        )
    return sources


# ----------------------------------------------------------------------
# 日付の見つけ方
# ----------------------------------------------------------------------
def parse_date(value: str) -> datetime | None:
    """文字列を日時として読む。読めなければ None。

    **時刻まで読む。** 日単位のファイルを 1 か月ぶんまとめる用途では、
    時刻を落とすと同じ日の中が元ファイルの並び順のまま残ってしまう。

    時刻が付いていない書き方は 00:00:00 として扱い、それでも読めなければ
    最後に「空白より前」だけで試す（後ろに余計な文字が付く出力への保険）。
    """
    text = " ".join((value or "").split()).replace("T", " ")
    if not text:
        return None

    for form in _DATETIME_FORMATS + _DATE_FORMATS:
        try:
            return datetime.strptime(text, form)
        except ValueError:
            continue

    head = text.split(" ")[0]
    if head != text:
        for form in _DATE_FORMATS:
            try:
                return datetime.strptime(head, form)
            except ValueError:
                continue
    return None


def _looks_like_dates(rows: list[dict[str, str]], column: str) -> bool:
    """その列の値が日時として読めるか、先頭の何行かで確かめる。"""
    values = [
        row.get(column, "") for row in rows if (row.get(column, "") or "").strip()
    ][:_SAMPLE_ROWS]
    if not values:
        return False
    parsed = sum(1 for value in values if parse_date(value) is not None)
    return parsed >= len(values) * _DATE_RATIO


def find_date_column(columns: list[str], rows: list[dict[str, str]]) -> str:
    """並べ替えに使う日時の列を、見出しの先頭付近から探す。

    列名を人に打たせないための自動判定。名前に「日付」などが入っている列を
    先に試し、駄目なら先頭付近の列の中身を実際に読んで判断する。
    """
    head = [column for column in columns[:_HEAD_COLUMNS] if column != SOURCE_COLUMN]
    named = [
        column
        for column in head
        if any(hint in column.lower() for hint in _DATE_NAME_HINTS)
    ]

    for column in named + [column for column in head if column not in named]:
        if _looks_like_dates(rows, column):
            return column
    return ""


def merge(
    sources: list[Path],
    output: Path,
    *,
    add_source: bool = True,
    min_rows: int = 1,
    sort_by_date: bool = True,
) -> MergeResult:
    """CSV をまとめて output に書く。

    見出しは最初のファイルの並びを基準にし、後のファイルにしか無い列は
    末尾に足す。無い値は空欄で埋める。列の食い違いは MergeResult に
    added_columns として残し、呼び出し側が気づけるようにする。

    sort_by_date のときは、見出しの先頭付近にある日時の列で古い順に
    並べ替える。読めなかった行は末尾に固めて残す（消さない）。
    """
    if not sources:
        raise MergeError("結合するファイルが指定されていません。")

    columns: list[str] = []
    added: list[str] = []
    merged: list[dict[str, str]] = []

    for index, path in enumerate(sources):
        fieldnames, rows, _ = _read(path)

        for name in fieldnames:
            if name in columns:
                continue
            columns.append(name)
            if index > 0:
                added.append(name)

        if add_source:
            for row in rows:
                row[SOURCE_COLUMN] = path.name

        merged.extend(rows)

    if add_source and SOURCE_COLUMN not in columns:
        columns.append(SOURCE_COLUMN)

    if len(merged) < min_rows:
        raise MergeError(
            f"結合結果が {len(merged)} 行しかありません（最低 {min_rows} 行）。"
            "対象アプリの出力が空の可能性があります。"
        )

    sort_column = ""
    unsorted = 0
    first_time: datetime | None = None
    last_time: datetime | None = None
    if sort_by_date:
        sort_column = find_date_column(columns, merged)
    if sort_column:
        # 日時として読めない行は捨てずに末尾へ回す。元の順序は保つ
        decorated = [
            (parse_date(row.get(sort_column, "")), position, row)
            for position, row in enumerate(merged)
        ]
        unsorted = sum(1 for when, _, _ in decorated if when is None)
        decorated.sort(
            key=lambda item: (
                item[0] is None,
                item[0] or datetime.min,
                item[1],
            )
        )
        merged = [row for _, _, row in decorated]

        times = [when for when, _, _ in decorated if when is not None]
        if times:
            first_time, last_time = times[0], times[-1]

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, restval="")
            writer.writeheader()
            writer.writerows(merged)
    except OSError as exc:
        raise MergeError(f"{output} に書けませんでした: {exc}") from exc

    return MergeResult(
        output=output,
        source_count=len(sources),
        row_count=len(merged),
        columns=tuple(columns),
        added_columns=tuple(added),
        sort_column=sort_column,
        unsorted_rows=unsorted,
        first_time=first_time,
        last_time=last_time,
    )


def count_rows(path: Path) -> int:
    """見出しを除いた行数を数える（ファイル検証用）。"""
    _, rows, _ = _read(path)
    return len(rows)
