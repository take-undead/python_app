# esp32cam

ESP32-CAM（AI-Thinker、`net_cam` ファームウェア）に接続して、
映像の表示・撮影・SD カード内の写真閲覧を行うアプリ。

## 実行

```powershell
python apps/esp32cam/main.py
```

## 使い方

1. `IP` にカメラのアドレスを入力する（SD カードの `config.txt` の `ip=` の値）。
2. `接続` で映像（MJPEG、ポート 81）の表示を開始する。
3. `撮影` でカメラ側の SD カードに高解像度（UXGA）の JPEG を保存する。
   `フラッシュ` にチェックを入れると GPIO4 の LED を点灯して撮影する。
4. `一覧更新` で SD カード内の写真を一覧表示し、`表示`（またはダブルクリック）で
   プレビューを開く。プレビューの `PC に保存` で PC 側にコピーできる。
5. `状態` で SD カードの容量とカメラの IP を確認する。

入力した IP は `settings.json` に保存され、次回起動時に復元される。
PC に保存した写真の既定の保存先は `downloads/` フォルダ。

## 通信するファームウェアのエンドポイント

| 用途 | エンドポイント |
| --- | --- |
| 映像 | `http://<ip>:81/stream`（multipart MJPEG） |
| 撮影 | `http://<ip>/capture?flash=on|off&t=YYYYMMDDHHmmss` |
| 写真一覧 | `http://<ip>/photos` |
| 写真取得 | `http://<ip>/photo?file=YYYYMM/YYYYMMDD_HHmmss.jpg` |
| 状態 | `http://<ip>/status` |

撮影時に PC の現在時刻を `t` パラメータで送るため、`/settime` は使わない
（ファイル名の日時はこの値で決まる）。

## 構成

```
main.py                エントリポイント
ui/main_window.py      メイン画面（映像・操作・写真一覧）
ui/photo_preview.py    写真のプレビューウィンドウ
ui/imaging.py          画像の変換・拡大縮小
logic/api.py           HTTP API クライアント（ポート 80）
logic/stream.py        MJPEG 受信（ポート 81）
logic/settings.py      IP アドレスの保存・復元
```
