# webpin

ESP32（TTGO T-Display）から WebSocket でピン情報を受け取り、
リアルタイムにグラフ表示しながら CSV に記録するアプリ。

```powershell
python apps/webpin/main.py
```

## 接続先

マイコン側は WiFi 接続後に `ws://<IP>/ws` で待ち受ける。
`T_Display_host/include/config.h` の設定に対応する既定値は次の 2 つ。

| 接続先 | 備考 |
| --- | --- |
| `ws://192.168.1.136/ws` | `STATIC_IP`（既定） |
| `ws://t-iot_mobile.local/ws` | `HOSTNAME` の mDNS 名 |

接続先の欄には `192.168.1.136` のようにホスト名だけを入れてもよい
（`ws://` と `/ws` は自動で補う）。接続が切れた場合は 3 秒ごとに自動で再接続する。

## 受信する内容

マイコンは約 200ms 間隔（`WS_UPDATE_INTERVAL_MS`）で `state` メッセージを送る。
これを次の名前の系列に展開して、グラフと CSV の列に使う。

| 系列名 | 内容 |
| --- | --- |
| `AIN1(GP36).raw` 〜 `AIN4(GP33).raw` | ADC の生値（0〜4095） |
| `AIN1(GP36).volt` 〜 `AIN4(GP33).volt` | 3.3V 換算した電圧 |
| `DIN1(GP37)` / `DIN2(GP38)` | デジタル入力（0 / 1） |
| `DOUT1` 〜 `DOUT5` | デジタル出力の状態（0 / 1） |
| `audio.playing` / `audio.freq` / `audio.volume` | I2S オーディオの状態 |

`ack` など `state` 以外のメッセージは記録しない。

## 操作

- **接続 / 切断** — 受信の開始と停止。
- **記録開始 / 記録停止** — CSV への記録。保存先は既定で `apps/webpin/logs/`。
- **クリア** — グラフと最新値の履歴を消す（記録中のファイルはそのまま）。
- **表示範囲** — グラフの横軸を直近 10 秒〜全期間で切り替える。
- **右のピン一覧** — チェックで系列の表示・非表示を切り替える。最新値も表示する。
  桁の違う系列を同じ軸に載せると読みにくいため、初期状態では `.volt` と
  デジタル入出力だけを表示する。

## CSV の形式

`時刻, 経過秒, <系列名...>` の順。Excel で開いても日本語が化けないよう
BOM 付き UTF-8（`utf-8-sig`）で書き出す。

```csv
時刻,経過秒,AIN1(GP36).raw,AIN1(GP36).volt,...,DIN1(GP37),...
2026-08-17 11:11:44.283,0.000,2048,1.65,...,0,...
```

列は記録を開始した最初の 1 件で確定する。途中で新しいピンが現れた場合、
その列は記録されず、記録停止時に警告を出す。

## 構成

| ファイル | 役割 |
| --- | --- |
| `logic/protocol.py` | 受信 JSON → 系列名と値の辞書。**書式が変わったらここを直す** |
| `logic/ws_client.py` | WebSocket 受信スレッドと自動再接続 |
| `logic/series.py` | グラフ用の時系列バッファ（既定 3000 点／系列） |
| `logic/recorder.py` | CSV への書き出し |
| `ui/chart.py` | `tk.Canvas` に直接描く折れ線グラフ |
| `ui/main_window.py` | 画面全体と受信の取り込み |

## 依存

`websocket-client`（リポジトリ直下の `requirements.txt` に含む）。
グラフは matplotlib を使わず `tk.Canvas` に直接描いている。

## 未実装

マイコンは `set_dout` / `play_tone` / `stop_audio` のコマンドも受け付ける。
送信口は `PinClient.send()` として用意してあるが、UI からの操作は未実装。
