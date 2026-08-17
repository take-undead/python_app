"""受信したピン値を CSV ファイルに記録する。

このモジュールは Tkinter に依存しない。
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import IO, Any

from logic.protocol import Sample

# 保存する時刻の書式（ミリ秒まで）
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


class RecorderError(Exception):
    """ログファイルの書き出しに失敗したときに送出する。"""


def default_log_path(directory: Path) -> Path:
    """`log_20260817_153000.csv` のような、時刻入りの保存先を作る。"""
    return directory / f"log_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"


class CsvRecorder:
    """Sample を 1 行ずつ CSV に追記する。

    列は最初の Sample のピン名で決まる。以降に現れた未知のピンは記録しない
    （列がずれると後の解析が壊れるため）。未知のピンは unknown_pins に残す。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: IO[str] | None = None
        self._writer: Any = None  # csv.writer() が返すライタ
        self._columns: list[str] = []
        self._row_count = 0
        self._unknown_pins: set[str] = set()
        self._start: float | None = None  # 経過秒の基準（最初の 1 件の時刻）

    @property
    def path(self) -> Path:
        return self._path

    @property
    def row_count(self) -> int:
        return self._row_count

    @property
    def columns(self) -> list[str]:
        return list(self._columns)

    @property
    def unknown_pins(self) -> set[str]:
        return set(self._unknown_pins)

    def is_open(self) -> bool:
        return self._file is not None

    def open(self) -> None:
        """ファイルを開く。失敗時は RecorderError。"""
        if self._file is not None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # newline="" は csv モジュールの作法（Windows で空行が入るのを防ぐ）。
            # utf-8-sig は BOM 付き UTF-8。Excel で開いても日本語が化けないようにする
            self._file = self._path.open("w", encoding="utf-8-sig", newline="")
        except OSError as exc:
            raise RecorderError(f"ログファイルを開けませんでした: {exc}") from exc
        self._writer = csv.writer(self._file)

    def write(self, sample: Sample) -> None:
        """1 件記録する。最初の 1 件で見出し行も書く。"""
        writer = self._writer
        file = self._file
        if writer is None or file is None:
            raise RecorderError("ログファイルが開かれていません。")

        if not self._columns:
            self._columns = list(sample.values)
            self._start = sample.timestamp
            self._write_row(writer, ["時刻", "経過秒", *self._columns])

        for pin in sample.values:
            if pin not in self._columns:
                self._unknown_pins.add(pin)

        timestamp = dt.datetime.fromtimestamp(sample.timestamp)
        elapsed = sample.timestamp - (self._start or sample.timestamp)
        row = [
            timestamp.strftime(_TIME_FORMAT)[:-3],
            f"{elapsed:.3f}",
            *(self._format(sample.values.get(pin)) for pin in self._columns),
        ]
        self._write_row(writer, row)
        self._row_count += 1

    def close(self) -> None:
        """ファイルを閉じる。開いていなければ何もしない。"""
        file = self._file
        self._file = None
        self._writer = None
        if file is not None:
            try:
                file.close()
            except OSError as exc:
                raise RecorderError(f"ログファイルを閉じられませんでした: {exc}") from exc

    def flush(self) -> None:
        """書き込み途中の内容をディスクに反映する。"""
        if self._file is not None:
            self._file.flush()

    @staticmethod
    def _format(value: float | None) -> str:
        if value is None:
            return ""
        # 整数として表せる値は 512.0 ではなく 512 と書く
        if value == int(value):
            return str(int(value))
        return repr(value)

    @staticmethod
    def _write_row(writer: Any, row: list[str]) -> None:
        try:
            writer.writerow(row)
        except OSError as exc:
            raise RecorderError(f"ログの書き込みに失敗しました: {exc}") from exc
