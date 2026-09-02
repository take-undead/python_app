# esp32cam

ESP32-CAM（AI-Thinker、`net_cam` ファームウェア）**6 台**に接続して、
映像の同時表示・撮影・SD カード内の写真閲覧を行うアプリ。

## 実行

```powershell
python apps/esp32cam/main.py
```

**使う人に渡すマニュアルは [MANUAL.md](MANUAL.md)。** 画面写真と PDF は次で作る。

```powershell
python tools/make_manual_shots.py esp32cam               # images/ に画面写真
python tools/make_manual_pdf.py apps/esp32cam/MANUAL.md  # build/docs/ に PDF
```

画面を変えたら MANUAL.md も直し、写真を撮り直して PDF を作り直すこと。
**撮影にカメラは要らない**。台ごとに色を変えた見本の映像を `_show_frame()` に渡し、
ボタンの状態だけ接続中のものに合わせている（`tools/make_manual_shots.py` の
`_demo_scene()`）。6 台そろっている保証が無く、つながってもその場の風景が
そのまま載るため。

## 画面

カメラ 6 台分のタイルを 3 列 × 2 行で並べる。各タイルに IP 入力欄と
`接続` / `切断` があり、カメラごとに個別に接続できる。

撮影・写真一覧・状態取得の対象は **タイルをクリックして選んだ 1 台**
（青枠が付き、右パネルに「対象: カメラ n」と表示される）。

## 使い方

1. 各タイルの IP を確認する（SD カードの `config.txt` の `ip=` の値）。
   初期値はカメラ 1 が `192.168.1.141` で、以降 1 ずつ増える。
2. `全カメラ接続` で 6 台の映像（MJPEG、ポート 81）をまとめて表示する。
   1 台だけならタイルの `接続` を使う。接続に失敗した台はまとめて 1 つの
   ダイアログで通知される。
3. 対象にしたいカメラのタイルをクリックして選ぶ。
4. `撮影` で選択中のカメラの SD カードに高解像度（UXGA）の JPEG を保存する。
   `全カメラ撮影` なら 6 台へ同時に撮影を指示する。
   `フラッシュ` にチェックを入れると GPIO4 の LED を点灯して撮影する。
5. `一覧更新` で選択中のカメラの写真を一覧表示し、`表示`（またはダブルクリック）で
   プレビューを開く。プレビューの `PC に保存` で PC 側にコピーできる。
   プレビュー左右端の `◀` `▶`（または ← → キー）で、一覧の前後のファイルに
   切り替えられる。先頭・末尾ではボタンが無効になる。
6. `状態` で選択中のカメラの SD カード容量と IP を確認する。

入力した 6 台分の IP は `settings.json` に保存され、次回起動時に復元される。
PC に保存した写真の既定の保存先は `downloads/` フォルダ。

映像の受信中に切れたカメラは、6 台分のダイアログが積み重なるのを避けるため、
そのタイルとステータスバーにだけエラーを表示する。

## 実行ファイル化

```powershell
python tools/build.py esp32cam            # 1 ファイルの exe（build/dist/esp32cam.exe）
python tools/build.py esp32cam --onedir   # 起動を速くしたい場合
```

`--onefile` は OpenCV を含むため約 67MB になり、起動のたびに展開が走って
2〜3 秒かかる。配布より起動の速さを優先するなら `--onedir` を使う。

exe 版の `settings.json` と `downloads/` は **exe と同じフォルダ** に作られる
（`logic/paths.py` の `data_dir()` が frozen かどうかで切り替える）。
`__file__` 起点のままだと `--onefile` の一時展開フォルダに保存され、
終了時に消えてしまう。

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
ui/main_window.py      メイン画面（タイルの配置・操作・写真一覧）
ui/camera_tile.py      カメラ 1 台分のタイル（IP 入力・接続・映像表示）
ui/photo_preview.py    写真のプレビューウィンドウ（前後のファイルへ切り替え）
ui/imaging.py          画像の変換・拡大縮小
logic/api.py           HTTP API クライアント（ポート 80）
logic/stream.py        MJPEG 受信（ポート 81）
logic/settings.py      6 台分の IP アドレスの保存・復元
logic/paths.py         データの保存先（通常実行と exe で切り替える）

MANUAL.md              使う人向けの操作マニュアル（PDF の原本）
images/                マニュアルに載せる画面写真（tools/make_manual_shots.py が作る）
```

台数は `logic/settings.py` の `CAMERA_COUNT`、列数は `ui/main_window.py` の
`_COLUMNS` で変えられる。

映像の更新は `MainWindow` の `after()` 1 本にまとめ、各タイルの `poll()` を
順に呼ぶ。`MjpegStream.take_frame()` は前回以降に届いたフレームだけを返すため、
更新の無いカメラでは画像変換をしない。
