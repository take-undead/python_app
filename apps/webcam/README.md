# webcam — Web カメラビューア

接続されている Web カメラの映像を表示し、静止画を保存する Tkinter アプリ。

**使う人に渡すマニュアルは [MANUAL.md](MANUAL.md)。** 画面写真と PDF は次で作る。

```powershell
python tools/make_manual_shots.py webcam               # images/ に画面写真
python tools/make_manual_pdf.py apps/webcam/MANUAL.md  # build/docs/ に PDF
```

画面を変えたら MANUAL.md も直し、写真を撮り直して PDF を作り直すこと。
**撮影ではカメラを開かない**（開発機の部屋がそのまま載るため）。映像が出ている
状態の写真は、見本のカラーバーを流し込んで撮っている。

## 実行

```powershell
.\.venv\Scripts\Activate.ps1
python apps/webcam/main.py
```

## 機能

- **検出** — 接続されているカメラを調べ、カメラ番号の候補を絞り込む（数秒かかる）
- **開始 / 停止** — 選んだカメラの映像表示を開始・停止する
- **撮影** — 表示中のフレームを `apps/webcam/captures/` に PNG で保存する

## 構成

| パス | 役割 |
| --- | --- |
| `main.py` | ウィンドウ生成とアプリ起動 |
| `ui/main_window.py` | 画面。映像の描画、ボタン操作 |
| `logic/camera.py` | カメラ取得と画像保存。Tkinter に依存しない |
| `captures/` | 撮影した静止画の保存先（Git 管理外、実行時に自動生成） |
| `MANUAL.md` | 使う人向けの操作マニュアル（PDF の原本） |
| `images/` | マニュアルに載せる画面写真（`tools/make_manual_shots.py` が作る） |

映像の取得は `logic.camera.CameraCapture` がバックグラウンドスレッドで行い、
UI は `after()` で最新フレームだけを取り出して描画する。

## 依存

- `opencv-python` — カメラ取得
- `Pillow` — OpenCV のフレームを Tkinter で表示できる形式に変換

## 注意

- 他のアプリ（Teams、Zoom など）がカメラを使用中だと開けない。
- 初回起動時に Windows のカメラアクセス許可を求められることがある。
  拒否されている場合は「設定 → プライバシーとセキュリティ → カメラ」で許可する。
