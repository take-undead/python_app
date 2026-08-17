"""受信したピン値を、グラフ描画用にピンごとの時系列として保持する。

このモジュールは Tkinter に依存しない。
"""

from __future__ import annotations

from collections import deque

from logic.protocol import Sample

# ピンごとに保持する最大点数。これを超えた分は古い方から捨てる
DEFAULT_CAPACITY = 3000


class SeriesStore:
    """ピン名ごとの (経過秒, 値) の履歴。

    最初の Sample の時刻を 0 秒として、経過秒で保持する。
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = capacity
        self._series: dict[str, deque[tuple[float, float]]] = {}
        self._start: float | None = None
        self._count = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def sample_count(self) -> int:
        """受信した Sample の総数。"""
        return self._count

    def pins(self) -> list[str]:
        """観測したピン名を、最初に現れた順で返す。"""
        return list(self._series)

    def add(self, sample: Sample) -> None:
        if self._start is None:
            self._start = sample.timestamp
        elapsed = sample.timestamp - self._start

        for pin, value in sample.values.items():
            points = self._series.get(pin)
            if points is None:
                points = deque(maxlen=self._capacity)
                self._series[pin] = points
            points.append((elapsed, value))

        self._count += 1

    def points(self, pin: str) -> list[tuple[float, float]]:
        """指定したピンの (経過秒, 値) を古い順で返す。"""
        points = self._series.get(pin)
        return list(points) if points is not None else []

    def latest(self, pin: str) -> float | None:
        """指定したピンの最新値。まだ 1 点もなければ None。"""
        points = self._series.get(pin)
        if not points:
            return None
        return points[-1][1]

    def time_range(self) -> tuple[float, float]:
        """全ピンを通した経過秒の最小・最大。データがなければ (0.0, 0.0)。"""
        starts: list[float] = []
        ends: list[float] = []
        for points in self._series.values():
            if points:
                starts.append(points[0][0])
                ends.append(points[-1][0])
        if not starts:
            return (0.0, 0.0)
        return (min(starts), max(ends))

    def value_range(self, pins: list[str]) -> tuple[float, float]:
        """指定ピンの値の最小・最大。データがなければ (0.0, 1.0)。"""
        values = [value for pin in pins for _, value in self._series.get(pin, ())]
        if not values:
            return (0.0, 1.0)
        low, high = min(values), max(values)
        if low == high:
            # 平坦なデータでも線が見えるよう、上下に余白を作る
            margin = abs(low) * 0.1 or 1.0
            return (low - margin, high + margin)
        return (low, high)

    def clear(self) -> None:
        self._series.clear()
        self._start = None
        self._count = 0
