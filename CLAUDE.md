# winapp — VSCode で UI 付き Python アプリを作るルール

Windows 上の VSCode で開発する、Tkinter 製デスクトップアプリ置き場。
1 リポジトリに複数のアプリを `apps/` 配下に並べるマルチアプリ構成。

## 基本方針

- UI ライブラリは **Tkinter**（標準ライブラリ）を使う。PyQt / PySide / Kivy / Electron 等は使わない。
- 仮想環境は **`.venv`**（Python 3.10.6）をリポジトリ直下に 1 つ置き、全アプリで共有する。グローバル環境にインストールしない。
- アプリは 1 つにつき `apps/<アプリ名>/` フォルダを 1 つ作る。
- 各アプリのエントリポイントは `apps/<アプリ名>/main.py`。
- 実行コマンドは **`python apps/<アプリ名>/main.py`**（リポジトリ直下から、`.venv` を有効化した状態で実行）。

## 環境

```powershell
# 仮想環境の有効化（PowerShell）
.\.venv\Scripts\Activate.ps1

# 実行（常にリポジトリ直下から）
python apps/webcam/main.py

# パッケージ追加時
pip install <package>
pip freeze > requirements.txt
```

- 追加依存を入れたら必ずリポジトリ直下の `requirements.txt` を更新する。
- 依存は全アプリ共有。特定アプリだけが必要とする重い依存が出た場合のみ、そのアプリ配下に個別の venv を切ることを検討する。
- `.venv/` は Git 管理対象外にする（`.gitignore` に記載）。

## VSCode 設定

- Python インタープリタは `.venv\Scripts\python.exe` を選択する（`Ctrl+Shift+P` → `Python: Select Interpreter`）。
- `.vscode/settings.json` に `"python.defaultInterpreterPath": ".venv\\Scripts\\python.exe"` を設定する。
- デバッグ実行は `.vscode/launch.json` に**アプリごとの構成**を用意する。構成名はアプリ名にし、`"program"` に各アプリの `main.py` を指定、`"cwd"` は `"${workspaceFolder}"` にする。
  - 現在開いているファイルを起点にしたい場合は `"program": "${file}"` の汎用構成を 1 つ足しておくとよい。
- コンソールを出さずに配布・起動したい場合のみ `.venv\Scripts\pythonw.exe` を使う。開発中は `python.exe` を使い、エラー出力を必ず見る。

## ファイル構成

```
.venv/                   # 全アプリ共有（Git 管理外）
requirements.txt         # 全アプリ共有の依存
.vscode/
tools/
  new_app.py             # 新しいアプリの雛形を生成する
  build.py               # アプリを実行ファイルにまとめる（成果物は build/ 配下）
  make_shortcut.py       # エクスプローラーから起動するショートカットを作る
build/                   # ビルド生成物（Git 管理外）
common/                  # 複数アプリで使う共通コード（必要になってから作る）
apps/
  webcam/                # アプリ 1 つにつき 1 フォルダ
    main.py              # エントリポイント。アプリ起動のみを担当
    ui/                  # 画面・ウィジェット（見た目）
    logic/               # 業務ロジック（Tkinter に依存しないこと）
    README.md            # そのアプリが何をするか（任意）
```

## 既存のアプリ

| アプリ | 実行 | 概要 |
| --- | --- | --- |
| `webcam` | `python apps/webcam/main.py` | Web カメラの映像表示と静止画保存 |
| `webpin` | `python apps/webpin/main.py` | ESP32 のピン情報を WebSocket で受信し、グラフ表示と CSV ロギング |
| `esp32cam` | `python apps/esp32cam/main.py` | ESP32-CAM 6 台の MJPEG 映像同時表示、撮影指示、SD カード内の写真閲覧（**参照実装**） |
| `win_rpa` | `python apps/win_rpa/main.py` | Windows アプリを自動操作して CSV を出力させ、結合する（月次ルーティン向け。`--run` で無人実行） |

- アプリフォルダ名は英小文字とアンダースコアのみ（`python -m` でも扱えるようにするため）。
- **アプリ間で直接 import しない。** `apps/memo` から `apps/viewer` を参照しない。共有したくなったコードは `common/` に切り出す。
- 1 ファイルが大きくなったら画面単位でモジュールを分割する。
- ロジックは UI から切り離し、`logic/` 側は Tkinter を import しない。

## import とパスの扱い

- `python apps/webcam/main.py` で起動すると `apps/webcam/` が `sys.path` の先頭に入るため、アプリ内の import は `from ui.main_window import MainWindow` のように**アプリ内相対**で書く。
- 上記の import を Pylance に解決させるため、`.vscode/settings.json` の `python.analysis.extraPaths` にアプリのパスが必要（`tools/new_app.py` が自動で追加する）。
- `common/` を使うアプリは、`main.py` の先頭でリポジトリ直下を `sys.path` に追加してから `from common.xxx import ...` する。

  ```python
  import sys
  from pathlib import Path

  sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
  ```

- **カレントディレクトリに依存しない。** 実行時の cwd はリポジトリ直下になるため、設定ファイルやアセットは `Path(__file__).parent / "config.json"` のように `__file__` 起点で解決する。
- アプリが読み書きするデータファイルは、そのアプリのフォルダ配下に置く。
- ただし実行ファイル化する予定のアプリは `__file__` 起点だと破綻する。「ビルド（実行ファイル化）」を参照。

## Tkinter 実装ルール

- 画面は `tk.Frame` / `ttk.Frame` を継承したクラスとして実装し、手続き的なベタ書きにしない。
- ウィジェットは原則 `tkinter.ttk` を使う（Windows のネイティブ外観に合わせるため）。
- レイアウトは `grid` を基本とし、同一の親コンテナ内で `pack` と `grid` を混在させない。
- ウィンドウリサイズに追従させるため、`columnconfigure` / `rowconfigure` の `weight` を設定する。
- 状態は `StringVar` / `IntVar` / `BooleanVar` などの `Variable` で保持し、ウィジェットと双方向に結びつける。
- ウィジェットへの参照は `self` に保持する。ローカル変数のみだと画像などが GC される。
- 時間のかかる処理をイベントハンドラ内で直接実行しない。UI が固まるため `threading.Thread` に逃がし、UI 更新は `widget.after()` 経由でメインスレッドに戻す（具体的な書き方は「実装パターン」を参照）。
- 定期処理は `sleep` ではなく `after()` で行う。
- ダイアログは自作せず `tkinter.messagebox` / `tkinter.filedialog` を使う。
- 日本語表示が崩れないよう、フォントは `Meiryo UI` など Windows 標準の日本語フォントを明示指定する。
- `mainloop()` の呼び出しは、そのアプリの `main.py` の 1 箇所だけにする。

## コーディング規約

- Python 3.10 の文法まで（`match`、`X | None` 型ヒントは可）。
- 公開関数・メソッドには型ヒントを付ける。
- 文字列リテラル・コメント・UI ラベルは日本語で記述してよい。
- ファイル入出力は `encoding="utf-8"` を明示する。
- パス操作は `os.path` ではなく `pathlib.Path` を使う。
- 例外はハンドラ内で握りつぶさず、`messagebox.showerror` でユーザーに提示する。

## 新しいアプリを追加する手順

**手で作らず、必ず雛形ツールを使う。**

```powershell
python tools/new_app.py memo --title "メモ帳"
```

これで次が生成・更新される。

- `apps/memo/` の一式（`main.py` / `ui/main_window.py` / `ui/__init__.py` / `logic/__init__.py` / `README.md`）
- `.vscode/settings.json` の `python.analysis.extraPaths`
- `.vscode/launch.json` のデバッグ構成

生成された時点で起動する状態になっている。**まず `python apps/memo/main.py` で起動を確認してから中身を作り始める**（あとで動かなくなったとき、雛形の問題か自分の変更かを切り分けられる）。

そのあとの順序:

1. `logic/` に業務ロジックを書く。Tkinter を import しない。専用の例外型を定義する。
2. `ui/main_window.py` に画面を組む。
3. 依存を追加した場合は `requirements.txt` を更新する。
4. 本ファイルの「既存のアプリ」表に 1 行追加する。

既存アプリには手を入れない。共通化が必要になった時点で `common/` に切り出す。

## 実装パターン

毎回設計し直さない。以下は 3 つの参照実装で共通して使っている形で、雛形にも入っている。

**時間のかかる処理**（通信・ファイル入出力・カメラ）

`threading.Thread` に逃がし、結果を `queue.Queue` に入れ、`after()` で回している定期処理から取り出して UI に反映する。ワーカースレッドから直接ウィジェットを触らない。実行中はボタンを `disabled` にして多重実行を防ぐ。

```python
def _run(self, task, on_success, busy_message) -> None:
    if self._busy:
        return
    self._set_busy(True)
    self._status_var.set(busy_message)

    def worker() -> None:
        try:
            result = task()
        except AppError as exc:          # logic 側の専用例外
            self._queue.put((self._on_task_error, exc))
        else:
            self._queue.put((on_success, result))

    threading.Thread(target=worker, daemon=True).start()
```

**動き続けるスレッド**（映像受信など）

`take_error()` で「発生したエラーを 1 度だけ取り出す」形にし、UI 側は定期処理のたびに確認する。参考: `apps/esp32cam/logic/stream.py`。

**終了処理**

画面クラスに `shutdown()` を用意し、`after_cancel()` と スレッド停止をそこに集約する。`main.py` の `WM_DELETE_WINDOW` から呼ぶ。これを怠るとウィンドウを閉じてもプロセスが残る。

**例外**

`logic/` 側にアプリ固有の例外型を定義し（`CameraError` / `ApiError` / `StreamError`）、UI 側で捕捉して `messagebox.showerror` に出す。`except Exception` で受けない。

**画像表示**

`ImageTk.PhotoImage` は `self` に保持しないと GC される。表示領域に合わせた拡大縮小は縦横比を保つ。参考: `apps/esp32cam/ui/imaging.py`。

## 参照実装

新しいアプリを作るときは **`esp32cam` を見本にする。** 3 つの中で最も網羅的で、上記のパターンがすべて入っている。

| 見たいもの | 場所 |
| --- | --- |
| 非同期処理とキューの受け渡し | `apps/esp32cam/ui/main_window.py` |
| 動き続けるスレッドとエラー通知 | `apps/esp32cam/logic/stream.py` |
| HTTP クライアントと例外設計 | `apps/esp32cam/logic/api.py` |
| 設定の保存・復元 | `apps/esp32cam/logic/settings.py` |
| 別ウィンドウ（Toplevel） | `apps/esp32cam/ui/photo_preview.py` |
| 同じ部品を複数並べる画面 | `apps/esp32cam/ui/camera_tile.py` |

## エクスプローラーから起動する

```powershell
python tools/make_shortcut.py webpin --desktop            # デスクトップにも置く
python tools/make_shortcut.py webpin --console            # 起動しないときの原因調査用
python tools/make_shortcut.py webpin --name "ピン監視"
```

- **`.py` を直接ダブルクリックさせない。** `.venv` ではなくシステム側の Python が使われ、依存パッケージが無く失敗する。
- 作られる `.lnk` は `.venv\Scripts\pythonw.exe` を指し、作業場所はリポジトリ直下になる。コンソールは出ない。
- `.lnk` にはこの PC の絶対パスが入るため Git 管理対象外（`.gitignore` に記載）。PC ごとに作り直す。
- コンソールなしで起動すると、起動時の例外がどこにも出ずに「ダブルクリックしても何も起きない」状態になる。`main.py` 側で例外を捕まえてログとダイアログに出すこと。

## ビルド（実行ファイル化）

```powershell
python tools/build.py webpin              # 1 ファイルの exe（コンソールなし）
python tools/build.py webpin --console    # 起動しないときの原因調査用
python tools/build.py webcam --onedir     # OpenCV を使うアプリはこちら
```

- 実行ファイルを作るときは **必ず `tools/build.py` を通す**。PyInstaller を直接叩かない。
- 生成物はすべて `build/` 配下に出す（`build/dist/` 成果物、`build/work/` 中間ファイル、`build/spec/` spec）。リポジトリ直下を汚さない。
- `build/` は Git 管理対象外（`.gitignore` に記載）。成果物はコミットしない。
- PyInstaller はビルド時にしか使わないため `requirements.txt` に含めない。未導入なら `pip install pyinstaller`。
- アプリ内相対 import を解決するため `--paths apps/<アプリ名>` が必要。`tools/build.py` が自動で渡す。
- OpenCV を使うアプリ（`webcam` / `esp32cam`）は `--onefile` だと起動のたびに展開が走って数秒かかる。`--onedir` を使う。
- アプリの動作確認はビルド前に `python apps/<アプリ名>/main.py` で済ませる。exe を作る意味があるのは配布形態の検証だけ。
- 既定はコンソールなし。**exe が起動しない場合のみ `--console` で作り直す。** モジュールの同梱漏れやパス崩れは Python 実行では再現せず exe 起動時にしか出ないため、そのときだけ例外を読む必要がある。
- ビルド先の exe が実行中だと Windows が上書きを拒否する。`tools/build.py` が検出して PID を表示するので、ウィンドウを閉じてから再実行する。`--onefile` の exe は親子 2 プロセス構成のため、強制終了ではなくウィンドウの「×」で閉じること（親だけ落とすと子が残ってロックし続ける）。
- **`Path(__file__)` 起点のデータパスは exe 化すると壊れる。** `--onefile` は一時フォルダへ自己展開するので、設定やログの保存先がそこになり終了時に消える。実行ファイル化するアプリは `getattr(sys, "frozen", False)` で分岐し、`sys.executable` 起点に切り替える。

## 変更後の確認

- コードを変更したら、そのアプリを `python apps/<アプリ名>/main.py` で実際に起動し、UI が表示され操作できることを確認する。
- 共通コード（`common/`）を変更した場合は、それを使っている全アプリを起動して確認する。
- 起動確認なしに「完了」と報告しない。

### 人手を介さずに確認する方法

目視だけに頼らず、次の 2 つで自動的に確かめられる。検証用スクリプトはリポジトリに置かず、一時フォルダに書いて捨てる。

**画面が組み上がるかの確認** — `mainloop()` の代わりに `root.update()` を回し、ウィジェットの状態を読んで検証する。例外があればここで出る。

```python
root = tk.Tk()
window = MainWindow(root)
window.grid(row=0, column=0, sticky="nsew")
for _ in range(30):
    root.update()
print(window._status_var.get())
window.shutdown()
root.destroy()
```

**外部機器が要るアプリの確認** — 機器の応答を模した偽サーバを立てて、実機なしで通信経路まで検証する。ESP32-CAM の MJPEG ストリームと HTTP API を `socket` で模した例が有効だった（`logic/` が Tkinter に依存していないので、UI を通さず単体でも叩ける）。

このとき `messagebox` を差し替えておくと、ダイアログでスクリプトが止まらない。

```python
import ui.main_window as mw
errors: list[str] = []
mw.messagebox.showerror = lambda title, message, **kw: errors.append(f"{title}: {message}")
```
