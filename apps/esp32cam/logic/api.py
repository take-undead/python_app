"""ESP32-CAM の HTTP API（port 80）を呼び出すモジュール。

対応するファームウェアのエンドポイント:
    GET /capture?flash=on|off&t=YYYYMMDDHHmmss  撮影して SD カードに保存
    GET /photos                                 SD カード内の写真一覧
    GET /photo?file=YYYYMM/xxxxx.jpg            写真 1 枚をバイナリ取得
    GET /status                                 SD カード・WiFi の状態

このモジュールは Tkinter に依存しない。
すべての関数は通信をブロックするため、UI スレッドから直接呼ばないこと。
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request

# 写真一覧や撮影は SD カードアクセスを伴うため、やや長めに待つ
DEFAULT_TIMEOUT = 15.0

# MJPEG ストリームは API とは別ポートで動作する
STREAM_PORT = 81


class ApiError(Exception):
    """ESP32-CAM との通信、または ESP32-CAM 側の処理に失敗したときに送出する。"""


def normalize_host(host: str) -> str:
    """入力されたホスト表記を "192.168.1.141" 形式に整える。

    "http://192.168.1.141/" や "192.168.1.141:81" のような入力も受け付ける。
    """
    text = host.strip()
    if not text:
        raise ApiError("カメラの IP アドレスを入力してください。")

    # スキームやパスが付いていても IP（ホスト名）部分だけを取り出す
    if "//" in text:
        text = text.split("//", 1)[1]
    text = text.split("/", 1)[0]
    text = text.split(":", 1)[0]

    if not text:
        raise ApiError("カメラの IP アドレスが正しくありません。")
    return text


def stream_url(host: str) -> str:
    """MJPEG ストリームの URL を返す。"""
    return f"http://{normalize_host(host)}:{STREAM_PORT}/stream"


def _request(
    host: str,
    path: str,
    params: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bytes:
    """GET リクエストを送り、レスポンスボディをそのまま返す。"""
    url = f"http://{normalize_host(host)}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise ApiError(f"カメラがエラーを返しました（HTTP {exc.code}）: {url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ApiError(
            f"カメラに接続できませんでした: {url}\n"
            "IP アドレスと、カメラの電源・WiFi 接続を確認してください。"
        ) from exc


def _request_json(
    host: str,
    path: str,
    params: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """GET リクエストを送り、JSON を辞書として返す。"""
    body = _request(host, path, params, timeout)
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(f"カメラの応答を解釈できませんでした: {path}") from exc

    if not isinstance(data, dict):
        raise ApiError(f"カメラの応答が想定と異なります: {path}")

    if data.get("status") == "error":
        message = data.get("message", "原因不明")
        raise ApiError(f"カメラ側でエラーが発生しました: {message}")

    return data


def format_time(when: dt.datetime | None = None) -> str:
    """ファームウェアが要求する YYYYMMDDHHmmss 形式の文字列を返す。"""
    return f"{when or dt.datetime.now():%Y%m%d%H%M%S}"


def capture(host: str, flash: bool = False, when: dt.datetime | None = None) -> str:
    """撮影して SD カードに保存させ、保存先の相対パスを返す。

    戻り値は "YYYYMM/YYYYMMDD_HHmmss.jpg" 形式で、そのまま fetch_photo() に渡せる。
    PC の現在時刻を t パラメータで同時に送るため、時刻設定の往復は不要。
    """
    data = _request_json(
        host,
        "/capture",
        {"flash": "on" if flash else "off", "t": format_time(when)},
    )
    name = data.get("file")
    if not isinstance(name, str) or not name:
        raise ApiError("撮影はできましたが、保存先を取得できませんでした。")
    return name


def list_photos(host: str) -> list[str]:
    """SD カード内の写真一覧（新しい順）を返す。"""
    data = _request_json(host, "/photos")
    files = data.get("files")
    if not isinstance(files, list):
        raise ApiError("写真一覧を取得できませんでした。")
    return [name for name in files if isinstance(name, str)]


def fetch_photo(host: str, name: str) -> bytes:
    """写真 1 枚を JPEG バイナリとして取得する。"""
    return _request(host, "/photo", {"file": name}, timeout=30.0)


def fetch_status(host: str) -> dict:
    """SD カード・WiFi の状態を取得する。"""
    return _request_json(host, "/status", timeout=10.0)


def describe_status(status: dict) -> str:
    """/status の応答を 1 行の日本語テキストに整形する。"""
    sd = status.get("sd", {})
    if not isinstance(sd, dict) or not sd.get("mounted"):
        return "SD カード: 未マウント"

    card_type = sd.get("type", "不明")
    total_mb = sd.get("total_mb", 0)
    used_mb = sd.get("used_mb", 0)
    wifi = status.get("wifi", "不明")
    return (
        f"SD カード: {card_type} / 使用 {used_mb}MB / 全体 {total_mb}MB"
        f"  IP: {wifi}"
    )
