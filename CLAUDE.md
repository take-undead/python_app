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
  build.py               # アプリを実行ファイルにまとめる（成果物は build/ 配下）
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
| `esp32cam` | `python apps/esp32cam/main.py` | ESP32-CAM の MJPEG 映像表示、撮影指示、SD カード内の写真閲覧 |

- アプリフォルダ名は英小文字とアンダースコアのみ（`python -m` でも扱えるようにするため）。
- **アプリ間で直接 import しない。** `apps/memo` から `apps/viewer` を参照しない。共有したくなったコードは `common/` に切り出す。
- 1 ファイルが大きくなったら画面単位でモジュールを分割する。
- ロジックは UI から切り離し、`logic/` 側は Tkinter を import しない。

## import とパスの扱い

- `python apps/webcam/main.py` で起動すると `apps/webcam/` が `sys.path` の先頭に入るため、アプリ内の import は `from ui.main_window import MainWindow` のように**アプリ内相対**で書く。
- 上記の import を Pylance に解決させるため、新しいアプリを作ったら `.vscode/settings.json` の `python.analysis.extraPaths` にそのアプリのパスを追加する。
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
- 時間のかかる処理をイベントハンドラ内で直接実行しない。UI が固まるため `threading.Thread` に逃がし、UI 更新は `widget.after()` 経由でメインスレッドに戻す。
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

1. `apps/<アプリ名>/` を作る。
2. `main.py` を置き、`ui/` `logic/` を必要に応じて作る。
3. `.vscode/launch.json` にデバッグ構成を、`.vscode/settings.json` の `python.analysis.extraPaths` にアプリのパスを追加する。
4. 依存を追加した場合は `requirements.txt` を更新する。
5. `python apps/<アプリ名>/main.py` で起動確認する。

既存アプリには手を入れない。共通化が必要になった時点で `common/` に切り出す。

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
