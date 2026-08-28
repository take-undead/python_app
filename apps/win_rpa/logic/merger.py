"""複数の CSV を 1 つにまとめる（Tkinter に依存しない）。

対象アプリが吐いた CSV を結合する。列の並びがファイルごとに違っても
見出しで対応づける。列が食い違ったまま黙って結合すると、
気づかないうちに壊れた集計表ができるため。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

# Excel が吐く CSV は UTF-8 BOM 付きのことが多いので、まず BOM 付きで試す
_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "cp932", "utf-8")


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


def find_sources(work_dir: Path, pattern: str) -> list[Path]:
    """結合対象のファイルを集める。"""
    if not work_dir.is_dir():
        raise MergeError(f"作業フォルダがありません: {work_dir}")

    sources = sorted(p for p in work_dir.glob(pattern) if p.is_file())
    if not sources:
        raise MergeError(
            f"{work_dir} に「{pattern}」に合うファイルがありません。"
            "対象アプリが CSV を出せていない可能性があります。"
        )
    return sources


def merge(
    sources: list[Path],
    output: Path,
    *,
    add_source: bool = True,
    min_rows: int = 1,
) -> MergeResult:
    """CSV をまとめて output に書く。

    見出しは最初のファイルの並びを基準にし、後のファイルにしか無い列は
    末尾に足す。無い値は空欄で埋める。列の食い違いは MergeResult に
    added_columns として残し、呼び出し側が気づけるようにする。
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
                row["元ファイル"] = path.name

        merged.extend(rows)

    if add_source and "元ファイル" not in columns:
        columns.append("元ファイル")

    if len(merged) < min_rows:
        raise MergeError(
            f"結合結果が {len(merged)} 行しかありません（最低 {min_rows} 行）。"
            "対象アプリの出力が空の可能性があります。"
        )

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
    )


def count_rows(path: Path) -> int:
    """見出しを除いた行数を数える（ファイル検証用）。"""
    _, rows, _ = _read(path)
    return len(rows)
