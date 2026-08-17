"""Web カメラの取得・保存を担当するモジュール。

このモジュールは Tkinter に依存しない（UI から切り離すため）。
"""

from __future__ import annotations

import threading
from pathlib import Path

import cv2
import numpy as np

# Windows で試すバックエンドの順番。
# DirectShow が最も起動が速いが、環境によっては使えないため順に試す。
_BACKENDS = (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY)

# フレーム取得に失敗しても、一時的なコマ落ちならすぐには止めない
_MAX_READ_FAILURES = 30


class CameraError(Exception):
    """カメラの初期化・取得・保存に失敗したときに送出する。"""


def _open_capture(device_index: int) -> cv2.VideoCapture | None:
    """使えるバックエンドを順に試してカメラを開く。開けなければ None。"""
    for backend in _BACKENDS:
        capture = cv2.VideoCapture(device_index, backend)
        if capture.isOpened():
            return capture
        capture.release()
    return None


def list_devices(max_index: int = 5) -> list[int]:
    """接続されているカメラのインデックス一覧を返す。

    実際にカメラを開いて確認するため、数秒かかることがある。
    UI スレッドから直接呼ばないこと。
    """
    available: list[int] = []
    for index in range(max_index):
        capture = _open_capture(index)
        if capture is not None:
            available.append(index)
            capture.release()
    return available


def save_frame(frame: np.ndarray, path: Path) -> None:
    """フレームを画像ファイルとして保存する。

    cv2.imwrite は日本語を含むパスで失敗するため、エンコードしてから書き出す。
    """
    ok, buffer = cv2.imencode(path.suffix, frame)
    if not ok:
        raise CameraError(f"画像のエンコードに失敗しました: {path.name}")
    try:
        path.write_bytes(buffer.tobytes())
    except OSError as exc:
        raise CameraError(f"画像の保存に失敗しました: {exc}") from exc


class CameraCapture:
    """バックグラウンドスレッドでフレームを取得し続ける。

    UI スレッドは latest_frame() で最新の 1 枚だけを取り出す。
    取得が途切れた場合は take_error() でエラー内容を受け取れる。
    """

    def __init__(self, device_index: int = 0) -> None:
        self._device_index = device_index
        self._capture: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._error: str | None = None

    @property
    def device_index(self) -> int:
        return self._device_index

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """カメラを開いて取得スレッドを開始する。失敗時は CameraError。"""
        if self._thread is not None:
            return

        capture = _open_capture(self._device_index)
        if capture is None:
            raise CameraError(
                f"カメラ {self._device_index} を開けませんでした。\n"
                "接続状態と、他のアプリがカメラを使用していないかを確認してください。"
            )

        self._capture = capture
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="camera-capture", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """取得スレッドを止めてカメラを解放する。"""
        self._stop_event.set()

        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

        capture = self._capture
        if capture is not None:
            capture.release()
        self._capture = None

        with self._lock:
            self._frame = None

    def latest_frame(self) -> np.ndarray | None:
        """最新フレーム（BGR）を返す。まだ 1 枚も取得できていなければ None。"""
        with self._lock:
            return self._frame

    def take_error(self) -> str | None:
        """発生したエラーを 1 度だけ取り出す。"""
        with self._lock:
            error = self._error
            self._error = None
        return error

    def _loop(self) -> None:
        capture = self._capture
        if capture is None:
            return

        failures = 0
        while not self._stop_event.is_set():
            ok, frame = capture.read()
            if not ok:
                failures += 1
                if failures >= _MAX_READ_FAILURES:
                    with self._lock:
                        self._error = (
                            "カメラからフレームを取得できなくなりました。\n"
                            "ケーブルの接続や、他のアプリの使用状況を確認してください。"
                        )
                    return
                continue

            failures = 0
            with self._lock:
                self._frame = frame
