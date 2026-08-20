"""マイコンの WebSocket サーバに接続し、state メッセージを受信し続ける。

受信はバックグラウンドスレッドで行い、UI スレッドは poll() で取り出すだけにする。
接続が切れた場合は自動で再接続する（マイコンの電源断や WiFi 断を想定）。

このモジュールは Tkinter に依存しない。
"""

from __future__ import annotations

import queue
import threading

import websocket

from logic.protocol import ProtocolError, Sample, parse_message

# config.h の STATIC_IP / HOSTNAME に対応する既定の接続先。
# 同じマイコンでも、つなぐネットワークによって 192.168.1.x と 192.168.2.x がある。
# ローカルネットワーク前提なので `192.168.` は省いた形で持つ
DEFAULT_HOST = "1.132"
ALT_HOST = "2.132"
MDNS_HOST = "t-iot_mobile.local"

# 接続先の入力候補。先頭を既定値として表示する
CANDIDATE_HOSTS = (DEFAULT_HOST, ALT_HOST, MDNS_HOST)

# `1.132` のような省略入力に補うプレフィックス
LOCAL_PREFIX = "192.168."

# 接続のタイムアウト（秒）
_CONNECT_TIMEOUT_S = 5.0

# 受信待ちのタイムアウト（秒）。停止指示への反応をこの間隔で確認する
_RECEIVE_TIMEOUT_S = 1.0

# 再接続を試みるまでの待ち時間（秒）
_RETRY_INTERVAL_S = 3.0

# 受信キューの上限。UI が詰まっても際限なくメモリを食わないようにする
_QUEUE_SIZE = 10000


class WsError(Exception):
    """接続・受信を続けられないときに送出する。"""


def _is_short_local_ip(host: str) -> bool:
    """`1.132` のような、192.168. を省いた 2 オクテット表記かどうか。"""
    parts = host.split(".")
    if len(parts) != 2:
        return False
    return all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)


def normalize_url(text: str) -> str:
    """入力された接続先を ws:// 形式の URL に整える。

    `192.168.1.132` や `t-iot_mobile.local` のようなホスト名だけの入力も受け付ける。
    ローカルネットワーク前提なので、`1.132` のように 192.168. を省いてもよい。
    """
    url = text.strip()
    if not url:
        raise WsError("接続先を入力してください。")
    if not url.startswith(("ws://", "wss://", "http://", "https://")) and (
        _is_short_local_ip(url.split("/")[0].split(":")[0])
    ):
        url = LOCAL_PREFIX + url
    if url.startswith("http://"):
        url = "ws://" + url[len("http://") :]
    elif url.startswith("https://"):
        url = "wss://" + url[len("https://") :]
    elif not url.startswith(("ws://", "wss://")):
        url = "ws://" + url
    if url.count("/") <= 2:  # スキーマの // だけ。パスが無ければ /ws を補う
        url = url.rstrip("/") + "/ws"
    return url


class PinClient:
    """WebSocket で受信した state を Sample に変換してキューに積む。

    UI スレッドは poll() で溜まった Sample をまとめて取り出し、
    take_status() で接続状態の変化を受け取る。
    """

    def __init__(self, url: str, auto_reconnect: bool = True) -> None:
        self._url = url
        self._auto_reconnect = auto_reconnect
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._queue: queue.Queue[Sample] = queue.Queue(maxsize=_QUEUE_SIZE)
        self._lock = threading.Lock()
        self._socket: websocket.WebSocket | None = None
        self._status: str | None = None
        self._connected = False
        self._skipped = 0

    @property
    def url(self) -> str:
        return self._url

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def skipped_messages(self) -> int:
        """解釈できずに読み飛ばしたメッセージ数。"""
        with self._lock:
            return self._skipped

    def start(self) -> None:
        """受信スレッドを開始する。接続は非同期に行う。"""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="ws-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """受信スレッドを止めて接続を閉じる。"""
        self._stop_event.set()

        # recv() の待ちを解くため、先にソケットを落とす
        with self._lock:
            socket = self._socket
        if socket is not None:
            try:
                socket.close()
            except (websocket.WebSocketException, OSError):
                pass  # 既に切れている場合。解放できれば十分

        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
        self._thread = None

        with self._lock:
            self._socket = None
            self._connected = False

    def send(self, message: str) -> None:
        """マイコンへコマンドを送る（set_dout / play_tone など）。"""
        with self._lock:
            socket = self._socket
            connected = self._connected
        if socket is None or not connected:
            raise WsError("接続していないため送信できません。")
        try:
            socket.send(message)
        except (websocket.WebSocketException, OSError) as exc:
            raise WsError(f"送信に失敗しました: {exc}") from exc

    def poll(self, limit: int = 500) -> list[Sample]:
        """キューに溜まった Sample を最大 limit 件まとめて取り出す。"""
        samples: list[Sample] = []
        for _ in range(limit):
            try:
                samples.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return samples

    def take_status(self) -> str | None:
        """接続状態の変化を 1 度だけ取り出す。無ければ None。"""
        with self._lock:
            status = self._status
            self._status = None
        return status

    # ------------------------------------------------------------------
    # 受信スレッド
    # ------------------------------------------------------------------
    def _set_status(self, text: str, connected: bool | None = None) -> None:
        with self._lock:
            self._status = text
            if connected is not None:
                self._connected = connected

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            socket = self._connect()
            if socket is None:
                if not self._auto_reconnect or self._stop_event.is_set():
                    return
                # 一定時間待ってから再接続。待機中も停止指示に反応できるようにする
                if self._stop_event.wait(_RETRY_INTERVAL_S):
                    return
                continue

            self._receive(socket)

            with self._lock:
                self._socket = None
                self._connected = False
            try:
                socket.close()
            except (websocket.WebSocketException, OSError):
                pass

            if not self._auto_reconnect or self._stop_event.is_set():
                return
            if self._stop_event.wait(_RETRY_INTERVAL_S):
                return

    def _connect(self) -> websocket.WebSocket | None:
        self._set_status(f"{self._url} に接続しています...", connected=False)
        try:
            socket = websocket.create_connection(
                self._url, timeout=_CONNECT_TIMEOUT_S
            )
        except (websocket.WebSocketException, OSError) as exc:
            self._set_status(f"接続できません（{exc}）", connected=False)
            return None

        socket.settimeout(_RECEIVE_TIMEOUT_S)
        with self._lock:
            self._socket = socket
        self._set_status(f"{self._url} に接続しました。", connected=True)
        return socket

    def _receive(self, socket: websocket.WebSocket) -> None:
        while not self._stop_event.is_set():
            try:
                text = socket.recv()
            except websocket.WebSocketTimeoutException:
                continue  # 受信が無いだけ。停止指示を確認して待ち直す
            except (websocket.WebSocketException, OSError) as exc:
                if not self._stop_event.is_set():
                    self._set_status(f"接続が切れました（{exc}）", connected=False)
                return

            if isinstance(text, bytes):
                text = text.decode("utf-8", errors="replace")
            if not text:
                # サーバから閉じられた
                if not self._stop_event.is_set():
                    self._set_status("接続が閉じられました。", connected=False)
                return

            try:
                sample = parse_message(text)
            except ProtocolError:
                with self._lock:
                    self._skipped += 1
                continue

            if sample is None:
                continue  # ack など、ロギング対象でないメッセージ

            try:
                self._queue.put_nowait(sample)
            except queue.Full:
                # UI 側の取り出しが追いつかない場合は古い 1 件を捨てて最新を残す
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(sample)
                except (queue.Empty, queue.Full):
                    pass
