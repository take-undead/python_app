"""ESP32（TTGO T-Display）から届く JSON を、ピン名と値の組に変換する。

マイコン側は約 200ms 間隔で次のような "state" メッセージを WebSocket で
ブロードキャストする（src/main.cpp の broadcastState() 参照）。

    {
      "type": "state",
      "ain":  [{"pin":36,"name":"AIN1(GP36)","raw":2048,"volt":1.65}, ...],
      "din":  [{"pin":37,"name":"DIN1(GP37)","value":0}, ...],
      "dout": [{"pin":17,"name":"DOUT1","state":false}, ...],
      "audio":{"playing":false,"freq":1000,"volume":200}
    }

これを次の名前でフラットな数値の辞書にする。ロギングの列名とグラフの系列名は
この名前をそのまま使う。

    AIN1(GP36).raw    0〜4095 の生値
    AIN1(GP36).volt   3.3V 換算した電圧
    DIN1(GP37)        0 / 1
    DOUT1             0 / 1
    audio.playing     0 / 1
    audio.freq        Hz
    audio.volume      0〜255

マイコン側の書式が変わったときは、原則このモジュールだけを直せば済む。
このモジュールは Tkinter に依存しない。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

# state メッセージ以外（ack など）はロギング対象にしない
STATE_TYPE = "state"

# グラフの初期表示で選んでおく系列の接尾辞。生値より電圧の方が読みやすい
DEFAULT_VISIBLE_SUFFIXES = (".volt",)


class ProtocolError(Exception):
    """受信メッセージを解釈できなかったときに送出する。"""


@dataclass
class Sample:
    """ある時刻に受信した 1 件分のピン値。"""

    timestamp: float
    values: dict[str, float] = field(default_factory=dict)
    raw: str = ""

    @property
    def pins(self) -> list[str]:
        return list(self.values)


def _to_float(value: object, context: str) -> float:
    """JSON の値を float にする。真偽値は 0/1 として扱う。"""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    raise ProtocolError(f"{context} を数値として解釈できません: {value!r}")


def _entry_name(entry: dict, fallback_prefix: str, index: int) -> str:
    """name があればそれを使い、無ければ pin 番号や連番で補う。"""
    name = entry.get("name")
    if isinstance(name, str) and name:
        return name
    pin = entry.get("pin")
    if isinstance(pin, int):
        return f"{fallback_prefix}(GP{pin})"
    return f"{fallback_prefix}{index + 1}"


def _read_array(
    message: dict, key: str, fields: dict[str, str], fallback_prefix: str
) -> dict[str, float]:
    """ain / din / dout の配列を、系列名 -> 値 の辞書に展開する。

    fields は {JSON のキー: 系列名の接尾辞} で、接尾辞が空なら名前をそのまま使う。
    """
    entries = message.get(key)
    if entries is None:
        return {}
    if not isinstance(entries, list):
        raise ProtocolError(f"{key} が配列ではありません。")

    values: dict[str, float] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ProtocolError(f"{key}[{index}] がオブジェクトではありません。")
        name = _entry_name(entry, fallback_prefix, index)
        for json_key, suffix in fields.items():
            if json_key not in entry:
                continue
            values[f"{name}{suffix}"] = _to_float(
                entry[json_key], f"{key}[{index}].{json_key}"
            )
    return values


def parse_message(text: str, timestamp: float | None = None) -> Sample | None:
    """受信した 1 メッセージを Sample に変換する。

    state 以外のメッセージ（ack など）は None を返す。
    解釈できないメッセージは ProtocolError を送出する。
    """
    stripped = text.strip()
    if not stripped:
        return None

    try:
        message = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"JSON として解釈できません: {exc.msg}") from exc

    if not isinstance(message, dict):
        raise ProtocolError("JSON オブジェクト以外は受け付けません。")

    if message.get("type") != STATE_TYPE:
        return None

    values: dict[str, float] = {}
    values.update(_read_array(message, "ain", {"raw": ".raw", "volt": ".volt"}, "AIN"))
    values.update(_read_array(message, "din", {"value": ""}, "DIN"))
    values.update(_read_array(message, "dout", {"state": ""}, "DOUT"))

    audio = message.get("audio")
    if isinstance(audio, dict):
        for key in ("playing", "freq", "volume"):
            if key in audio:
                values[f"audio.{key}"] = _to_float(audio[key], f"audio.{key}")

    if not values:
        return None

    stamp = time.time() if timestamp is None else timestamp
    return Sample(timestamp=stamp, values=values, raw=stripped)


def default_visible_pins(pins: list[str]) -> set[str]:
    """初期表示で有効にする系列を選ぶ。

    電圧・デジタル入出力だけを既定にし、0〜4095 の生値や audio は外す
    （桁が違う系列を同じ軸に載せると、他が潰れて読めなくなるため）。
    """
    visible = {
        pin
        for pin in pins
        if pin.endswith(DEFAULT_VISIBLE_SUFFIXES)
        or (pin.startswith(("DIN", "DOUT")) and "." not in pin)
    }
    return visible or set(pins)
