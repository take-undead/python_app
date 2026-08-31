"""ファイルをダブルクリックしたとき実際に走るものを求める（Tkinter に依存しない）。

対象の業務アプリが `.kww` のような**固有拡張子のファイル**で配られていることがある。
そのファイル自体は実行ファイルではないので、`Application.start()`（中身は
CreateProcess）にそのまま渡しても起動しない。CreateProcess は関連付けも
ショートカットも解決しないため。

そこで Windows がダブルクリックでやっていることを自前でたどり、実体の exe と
引数まで落としてから起動する。たどり方は 2 通り。

- 中身がシェルリンク（`.lnk` と同じ形式）→ リンク先と引数を読む
- そうでない                             → 関連付けから起動コマンドを読む

`os.startfile`（ShellExecute）に投げれば Windows が全部やってくれるが、それは
採らない。**起動したプロセスの PID が返らない**ので、`Runner` が `self._app` を
保持できず「アプリを閉じる」が窓の題名頼みになる。exe と引数まで落としておけば
今までどおり `Application.start()` が使え、画面にも「実際に走るコマンド」を出せる。
"""

from __future__ import annotations

import os
import winreg
from dataclasses import dataclass
from pathlib import Path

from logic import shortcuts

# CreateProcess にそのまま渡せる拡張子
RUNNABLE_SUFFIXES: tuple[str, ...] = (".exe", ".bat", ".cmd", ".com")

# ショートカットがショートカットを指す形をたどる上限
_MAX_DEPTH = 3

# 関連付けのコマンドに出てくる差し込み記号。%* は「残りの引数」なので空にする
_PATH_TOKENS: tuple[str, ...] = ("%1", "%L", "%l", "%D", "%~1")


class LaunchError(Exception):
    """起動方法を求められなかった。"""


@dataclass(frozen=True)
class LaunchTarget:
    """実際に走る exe と引数。"""

    exe: Path
    args: str
    work_dir: Path | None
    how: str  # どうやって求めたか。実行ログと画面に出す

    @property
    def command(self) -> str:
        """起動用のコマンドライン。"""
        if self.args:
            return f'"{self.exe}" {self.args}'
        return f'"{self.exe}"'


def resolve_launch(path: Path, *, _depth: int = 0) -> LaunchTarget:
    """このファイルを開いたときに実際に走るものを求める。

    exe / ショートカット / 関連付けのどれでも同じ形で返す。
    """
    if _depth > _MAX_DEPTH:
        raise LaunchError(f"{path.name} の起動方法が堂々巡りになりました。")
    if not path.is_file():
        raise LaunchError(f"ファイルが見つかりません: {path}")

    if path.suffix.lower() in RUNNABLE_SUFFIXES:
        return LaunchTarget(path, "", path.parent, "実行ファイル")

    # 拡張子ではなく中身で判断する。.kww のような固有拡張子のショートカットは
    # 拡張子で弾くと読めない
    if shortcuts.is_shell_link(path):
        # ShortcutError をそのまま外へ出さない。Runner は AutomationError しか
        # 手順の失敗として扱わないので、抜けると実行全体が例外で落ちる
        try:
            link = shortcuts.resolve(path)
        except shortcuts.ShortcutError as exc:
            raise LaunchError(str(exc)) from exc
        inner = resolve_launch(link.target, _depth=_depth + 1)
        return LaunchTarget(
            inner.exe,
            _join_args(inner.args, link.args),
            link.work_dir or inner.work_dir,
            "ショートカット",
        )

    exe, args = _association(path)
    return LaunchTarget(exe, args, exe.parent, "関連付け")


def _join_args(*parts: str) -> str:
    return " ".join(part for part in parts if part.strip()).strip()


# ----------------------------------------------------------------------
# 関連付け
# ----------------------------------------------------------------------
def _read(root: int, key: str, value: str = "") -> str:
    """レジストリの値を読む。無ければ空文字。"""
    try:
        with winreg.OpenKey(root, key) as handle:
            data, _ = winreg.QueryValueEx(handle, value)
    except OSError:
        return ""
    return str(data).strip()


def _progid(ext: str) -> str:
    """拡張子から ProgID を求める。

    「プログラムから開く」で選び直されていると `UserChoice` が優先される。
    HKCR だけ見ると、実際に起動するものと食い違う。
    """
    user_choice = _read(
        winreg.HKEY_CURRENT_USER,
        rf"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{ext}\UserChoice",
        "ProgId",
    )
    if user_choice:
        return user_choice
    return _read(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{ext}") or _read(
        winreg.HKEY_CLASSES_ROOT, ext
    )


def _open_command(base: str) -> str:
    """ProgID（または拡張子）から起動コマンドの雛形を読む。

    既定の動作は `open` とは限らないので、`shell` の既定値を先に見る。
    """
    verb = _read(winreg.HKEY_CLASSES_ROOT, rf"{base}\shell") or "open"
    for candidate in (verb, "open"):
        command = _read(winreg.HKEY_CLASSES_ROOT, rf"{base}\shell\{candidate}\command")
        if command:
            return command
    return ""


def _split_command(template: str) -> tuple[str, str]:
    """コマンドの雛形を「実行ファイル」と「引数」に分ける。"""
    text = template.strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        if end == -1:
            raise LaunchError(f"起動コマンドを読めませんでした: {template}")
        return text[1:end], text[end + 1 :].strip()
    head, _, rest = text.partition(" ")
    return head, rest.strip()


def expand_args(raw_args: str, path: Path) -> tuple[str, bool]:
    """関連付けの引数にファイルのパスを差し込む。

    レジストリの雛形は `"%1"` と**すでに引用符で囲まれている**ことが多い。
    そこに引用符付きのパスを入れると `""C:\\...""` になる。空白を含まない
    パスなら偶然通ってしまうが、空白があると引数が分割されて壊れる。
    囲み済みかどうかを見て、二重にしない。

    戻り値の 2 つめは、差し込み記号があったかどうか。
    """
    quoted = f'"{path}"'
    args = raw_args
    replaced = False

    for token in _PATH_TOKENS:
        if f'"{token}"' in args:
            args = args.replace(f'"{token}"', quoted)
            replaced = True
        elif token in args:
            args = args.replace(token, quoted)
            replaced = True

    # %* は「残りの引数」。渡すものが無いので消す
    args = args.replace('"%*"', "").replace("%*", "").strip()
    return args, replaced


def _association(path: Path) -> tuple[Path, str]:
    """関連付けから、実際に起動される exe と引数を求める。"""
    ext = path.suffix.lower()
    if not ext:
        raise LaunchError(f"{path.name} は実行ファイルではありません。")

    progid = _progid(ext)
    template = ""
    for base in (progid, ext):
        if base:
            template = _open_command(base)
            if template:
                break

    if not template:
        raise LaunchError(
            f"{ext} を開く方法が この PC に登録されていません。\n"
            "対象アプリの実行ファイルを直接選んでください。"
        )

    raw_exe, raw_args = _split_command(template)
    exe = Path(os.path.expandvars(raw_exe))
    if not exe.is_file():
        raise LaunchError(
            f"{ext} に登録された実行ファイルが見つかりません: {exe}\n"
            "対象アプリが入っていないか、場所が変わっています。"
        )

    args, replaced = expand_args(raw_args, path)

    # 差し込み記号が無い書き方（DDE を使う古いアプリなど）。付けないと
    # アプリだけ起動してファイルが開かれない
    if not replaced:
        args = _join_args(args, f'"{path}"')

    return exe, args
