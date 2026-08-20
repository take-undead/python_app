"""ESP32-CAM の MJPEG ストリーム（port 81）を受信するモジュール。

ファームウェアは multipart/x-mixed-replace で JPEG を送り続けるが、
各パートに Content-Length を付けないため、JPEG のマーカー
（SOI: FFD8 〜 EOI: FFD9）を探してフレームを切り出す。

このモジュールは Tkinter に依存しない。
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request

import cv2
import numpy as np

# ソケットの読み取り単位
_CHUNK_SIZE = 8192

# JPEG の開始・終了マーカー
_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"

# マーカーが見つからないまま溜まったデータを捨てる閾値（バイト）
_MAX_BUFFER = 1024 * 1024


class StreamError(Exception):
    """ストリームの接続・受信に失敗したときに送出する。"""


class MjpegStream:
    """バックグラウンドスレッドで MJPEG を受信し続ける。

    UI スレッドは latest_frame() で最新の 1 枚だけを取り出す。
    受信が途切れた場合は take_error() でエラー内容を受け取れる。
    """

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self._url = url
        self._timeout = timeout
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._frame_new = False
        self._error: str | None = None
        self._response = None

    @property
    def url(self) -> str:
        return self._url

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """接続を確立して受信スレッドを開始する。失敗時は StreamError。"""
        if self._thread is not None:
            return

        try:
            # 接続確立までは呼び出し側で待ち、失敗をその場で通知する
            self._response = urllib.request.urlopen(self._url, timeout=self._timeout)
        except (urllib.error.URLError, OSError) as exc:
            self._response = None
            raise StreamError(
                f"映像に接続できませんでした: {self._url}\n"
                "IP アドレスと、カメラの電源・WiFi 接続を確認してください。"
            ) from exc

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="esp32cam-stream", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """受信スレッドを止めて接続を閉じる。"""
        self._stop_event.set()

        # read() でブロックしているスレッドを解放するため先に閉じる
        response = self._response
        self._response = None
        if response is not None:
            try:
                response.close()
            except OSError:
                pass

        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

        with self._lock:
            self._frame = None
            self._frame_new = False

    def latest_frame(self) -> np.ndarray | None:
        """最新フレーム（BGR）を返す。まだ 1 枚も受信できていなければ None。"""
        with self._lock:
            return self._frame

    def take_frame(self) -> np.ndarray | None:
        """前回の取り出し以降に届いたフレームを返す。届いていなければ None。

        複数台を同時に描画するとき、変化が無いカメラの画像変換を省くために使う。
        """
        with self._lock:
            if not self._frame_new:
                return None
            self._frame_new = False
            return self._frame

    def take_error(self) -> str | None:
        """発生したエラーを 1 度だけ取り出す。"""
        with self._lock:
            error = self._error
            self._error = None
        return error

    # ------------------------------------------------------------------
    # 受信スレッド
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        response = self._response
        if response is None:
            return

        buffer = bytearray()
        try:
            while not self._stop_event.is_set():
                chunk = response.read(_CHUNK_SIZE)
                if not chunk:
                    self._set_error("カメラとの接続が切断されました。")
                    return

                buffer.extend(chunk)
                self._extract_frames(buffer)
        except OSError as exc:
            # stop() が閉じたことによる例外は正常終了として扱う
            if not self._stop_event.is_set():
                self._set_error(f"映像の受信が途切れました: {exc}")

    def _extract_frames(self, buffer: bytearray) -> None:
        """バッファから完成した JPEG を取り出してデコードする。"""
        while True:
            start = buffer.find(_SOI)
            if start < 0:
                # マーカーが無いデータが溜まり続けないよう捨てる
                if len(buffer) > _MAX_BUFFER:
                    del buffer[:-1]
                return

            end = buffer.find(_EOI, start + 2)
            if end < 0:
                del buffer[:start]
                if len(buffer) > _MAX_BUFFER:
                    buffer.clear()
                return

            jpeg = bytes(buffer[start : end + 2])
            del buffer[: end + 2]

            frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue  # 壊れたフレームは捨てて次を待つ
            with self._lock:
                self._frame = frame
                self._frame_new = True

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._error = message
