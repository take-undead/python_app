"""Markdown のマニュアルを PDF にする。

    python tools/make_manual_pdf.py apps/win_rpa/MANUAL.md

Markdown を印刷向けの HTML に組み立て、Microsoft Edge のヘッドレスモードで
PDF にする。追加のパッケージは要らない（Edge は Windows に最初から入っている）。

外部ライブラリを使わないのは、マニュアルを 1 本 PDF にするためだけに
requirements.txt を増やしたくないため。Markdown は自分たちで書くものなので、
使う記法だけを解釈できれば足りる。

対応している記法:
    # 見出し（h1〜h4）        ```コードブロック```      | 表 |
    - 箇条書き / 1. 番号付き   > 引用（注意書き）        ---（区切り線）
    **太字**  `コード`  [文字](リンク)
    ![説明](images/xxx.png)   ← **行に 1 つだけ**書く（文中には置けない）

画像は PDF に**埋め込む**（data URI にする）。相対パスのまま出力先の
build/docs/ に置くと、HTML から見たときにリンクが切れるため。
"""

from __future__ import annotations

import argparse
import base64
import html
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = ROOT / "build" / "docs"

# Edge の場所。どちらにも無ければ PATH を探す
EDGE_CANDIDATES: tuple[Path, ...] = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    / "Microsoft/Edge/Application/msedge.exe",
)


class ManualError(Exception):
    """マニュアルを PDF にできなかった。"""


# ----------------------------------------------------------------------
# インライン記法
# ----------------------------------------------------------------------
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _inline(text: str) -> str:
    """1 行ぶんのインライン記法を HTML にする。

    コード（`...`）を先に取り置いてから他を処理する。中に ** や [ ] が
    入っていても、コードとして書いたものは変換しないため。
    """
    spans: list[str] = []

    def stash(match: re.Match[str]) -> str:
        spans.append(html.escape(match.group(1)))
        return f"\x00{len(spans) - 1}\x00"

    text = _CODE.sub(stash, text)
    text = html.escape(text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _LINK.sub(r'<a href="\2">\1</a>', text)

    for index, code in enumerate(spans):
        text = text.replace(f"\x00{index}\x00", f"<code>{code}</code>")
    return text


# 前後に空白を入れない文字（和文の約物）
_NO_SPACE = "、。，．！？：；（）［］｛｝「」『』〈〉《》【】〔〕…‥・ー〜～　"


def _needs_space(left: str, right: str) -> bool:
    """折り返しをつなぐとき、その境目に空白が要るか。

    和文どうしなら要らない。**和文と欧文の境目には入れる**
    （このリポジトリの書き方に合わせる）。これをしないと、
    「写真を / PC から見る」で折り返した行が「写真をPC から見る」になる。
    """
    if not left or not right:
        return False
    if left in _NO_SPACE or right in _NO_SPACE:
        return False
    # 和文どうしのときだけ詰める
    return left.isascii() or right.isascii()


def _join(lines: list[str]) -> str:
    """折り返された行をつなぐ。"""
    result = ""
    for line in lines:
        line = line.strip()
        if not result:
            result = line
            continue
        result += (" " if _needs_space(result[-1], line[0]) else "") + line
    return result


# ----------------------------------------------------------------------
# ブロック記法
# ----------------------------------------------------------------------
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_ULIST = re.compile(r"^[-*]\s+(.*)$")
_OLIST = re.compile(r"^(\d+)\.\s+(.*)$")
_TABLE_RULE = re.compile(r"^\|[\s:|-]+\|$")
_IMAGE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)$")


def _figure(alt: str, src: str, base: Path) -> str:
    """画像を、中身ごと埋め込んだ <figure> にする。"""
    path = Path(src)
    if not path.is_absolute():
        path = base / path
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ManualError(
            f"画像を読めませんでした: {path}\n"
            "画面写真は tools/make_manual_shots.py で作ります。"
        ) from exc

    kind = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(data).decode("ascii")
    caption = f"<figcaption>{_inline(alt)}</figcaption>" if alt else ""
    return (
        f'<figure><img src="data:{kind};base64,{encoded}" '
        f'alt="{html.escape(alt)}">{caption}</figure>'
    )


def _slug(text: str, used: set[str]) -> str:
    """見出しから目次のリンク先を作る。"""
    base = re.sub(r"[^\w\u3040-\u30ff\u4e00-\u9fff]+", "-", text).strip("-").lower()
    base = base or "section"
    name, count = base, 2
    while name in used:
        name, count = f"{base}-{count}", count + 1
    used.add(name)
    return name


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _render(source: str, base: Path) -> tuple[list[str], list[tuple[int, str, str]]]:
    """Markdown を HTML のブロックの並びにする。

    戻り値は（HTML ブロック, 目次の項目）。目次は（階層, 表示, リンク先）。
    base は画像の相対パスを解決する起点（.md があるフォルダ）。
    """
    lines = source.replace("\r\n", "\n").split("\n")
    blocks: list[str] = []
    toc: list[tuple[int, str, str]] = []
    used: set[str] = set()
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        # コードブロック
        if stripped.startswith("```"):
            index += 1
            body: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                body.append(lines[index])
                index += 1
            index += 1
            blocks.append(f"<pre><code>{html.escape(chr(10).join(body))}</code></pre>")
            continue

        # 見出し
        heading = _HEADING.match(stripped)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2).strip()
            anchor = _slug(text, used)
            if level in (2, 3):
                # 目次は最初の見出しの「前」に入れるので、目印を先に置く
                blocks.append(f"\x01{len(toc)}")
                toc.append((level, text, anchor))
            blocks.append(f'<h{level} id="{anchor}">{_inline(text)}</h{level}>')
            index += 1
            continue

        # 画像（行に 1 つだけ書かれているもの）
        image = _IMAGE.match(stripped)
        if image:
            blocks.append(_figure(image.group(1), image.group(2), base))
            index += 1
            continue

        # 区切り線
        if set(stripped) == {"-"} and len(stripped) >= 3:
            blocks.append("<hr>")
            index += 1
            continue

        # 表
        if (
            stripped.startswith("|")
            and index + 1 < len(lines)
            and _TABLE_RULE.match(lines[index + 1].strip())
        ):
            header = _cells(stripped)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(_cells(lines[index].strip()))
                index += 1

            head = "".join(f"<th>{_inline(cell)}</th>" for cell in header)
            body_html = "".join(
                "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>"
                for row in rows
            )
            blocks.append(
                f"<table><thead><tr>{head}</tr></thead><tbody>{body_html}</tbody></table>"
            )
            continue

        # 引用（注意書き）
        if stripped.startswith(">"):
            quoted: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quoted.append(lines[index].strip().lstrip(">").strip())
                index += 1
            blocks.append(f"<blockquote><p>{_inline(_join(quoted))}</p></blockquote>")
            continue

        # 箇条書き（順序あり・なし）
        if _ULIST.match(stripped) or _OLIST.match(stripped):
            ordered = bool(_OLIST.match(stripped))
            items: list[list[str]] = []
            while index < len(lines):
                current = lines[index]
                text = current.strip()
                if not text:
                    # 空行のあとも項目が続くなら同じリストとして扱う
                    following = lines[index + 1].strip() if index + 1 < len(lines) else ""
                    if not (_ULIST.match(following) or _OLIST.match(following)):
                        break
                    index += 1
                    continue

                match = _OLIST.match(text) if ordered else _ULIST.match(text)
                if match:
                    items.append([match.group(2 if ordered else 1)])
                elif items and current.startswith((" ", "\t")):
                    items[-1].append(text)     # 折り返された続き
                else:
                    break
                index += 1

            tag = "ol" if ordered else "ul"
            body_html = "".join(f"<li>{_inline(_join(item))}</li>" for item in items)
            blocks.append(f"<{tag}>{body_html}</{tag}>")
            continue

        # 段落
        paragraph: list[str] = []
        while index < len(lines) and lines[index].strip():
            text = lines[index].strip()
            if (
                text.startswith(("#", ">", "|", "```"))
                or _ULIST.match(text)
                or _OLIST.match(text)
                or _IMAGE.match(text)
            ):
                break
            paragraph.append(text)
            index += 1
        if paragraph:
            blocks.append(f"<p>{_inline(_join(paragraph))}</p>")
        else:
            index += 1

    return blocks, toc


def _toc_html(toc: list[tuple[int, str, str]]) -> str:
    """目次を組み立てる。"""
    rows = "".join(
        f'<li class="lv{level}"><a href="#{anchor}">{_inline(text)}</a></li>'
        for level, text, anchor in toc
    )
    return f'<nav class="toc"><h2>目次</h2><ul>{rows}</ul></nav>'


# 印刷用のスタイル。日本語が崩れないよう Windows 標準のフォントを明示する
_STYLE = """
@page { size: A4; margin: 18mm 15mm 16mm; }

* { box-sizing: border-box; }
body {
  font-family: "Meiryo UI", Meiryo, "Yu Gothic UI", sans-serif;
  font-size: 10pt; line-height: 1.75; color: #1f2937;
  margin: 0; padding: 0;
}
code, pre {
  font-family: Consolas, "MS Gothic", monospace;
}

h1 {
  font-size: 21pt; line-height: 1.4; margin: 0 0 6mm;
  padding-bottom: 3mm; border-bottom: 2.5px solid #1f2937;
}
h2 {
  font-size: 14pt; margin: 9mm 0 3mm; padding: 1.5mm 0 1.5mm 3mm;
  border-left: 4px solid #1f2937; background: #f3f4f6;
  break-after: avoid; break-inside: avoid;
}
h3 {
  font-size: 11.5pt; margin: 6mm 0 2mm; padding-bottom: 1mm;
  border-bottom: 1px solid #d1d5db;
  break-after: avoid; break-inside: avoid;
}
h4 { font-size: 10.5pt; margin: 5mm 0 1.5mm; break-after: avoid; }

p { margin: 0 0 3mm; }
strong { font-weight: 700; }
a { color: #1d4ed8; text-decoration: none; }
hr { border: 0; border-top: 1px solid #e5e7eb; margin: 7mm 0; }

ul, ol { margin: 0 0 3mm; padding-left: 6mm; }
li { margin-bottom: 1mm; break-inside: avoid; }

code {
  font-size: 9pt; background: #f3f4f6; border: 1px solid #e5e7eb;
  border-radius: 2px; padding: 0 1mm;
}
pre {
  font-size: 8pt; line-height: 1.5; background: #f8f9fa;
  border: 1px solid #e5e7eb; border-left: 3px solid #9ca3af;
  border-radius: 2px; padding: 3mm; margin: 0 0 4mm;
  white-space: pre; overflow: hidden;
}
pre code { font-size: inherit; background: none; border: 0; padding: 0; }

/* 画面写真 */
figure {
  margin: 0 0 5mm; text-align: center; break-inside: avoid;
}
figure img {
  max-width: 100%; border: 1px solid #d1d5db; border-radius: 2px;
}
figcaption {
  font-size: 8.5pt; color: #6b7280; margin-top: 1.5mm; text-align: center;
}

blockquote {
  margin: 0 0 4mm; padding: 2.5mm 3mm; background: #fffbeb;
  border: 1px solid #fde68a; border-left: 4px solid #d97706;
  border-radius: 2px; break-inside: avoid;
}
blockquote p { margin: 0; }

table {
  width: 100%; border-collapse: collapse; margin: 0 0 4mm; font-size: 9pt;
}
thead { display: table-header-group; }
th, td {
  border: 1px solid #d1d5db; padding: 1.5mm 2mm;
  text-align: left; vertical-align: top;
}
th { background: #f3f4f6; font-weight: 700; white-space: nowrap; }
tr { break-inside: avoid; }
td code { font-size: 8.5pt; }

/* 目次 */
.toc {
  break-after: page; border: 1px solid #d1d5db; border-radius: 2px;
  padding: 4mm 6mm 5mm; background: #fafafa;
}
.toc h2 {
  margin: 0 0 3mm; padding: 0; border: 0; background: none; font-size: 13pt;
}
.toc ul { list-style: none; padding: 0; margin: 0; }
.toc li { margin-bottom: 0.8mm; }
.toc a { color: #1f2937; }
.toc .lv3 { padding-left: 6mm; font-size: 9pt; color: #4b5563; }
"""


def to_html(source: str, title: str, base: Path | None = None) -> str:
    """Markdown を、印刷向けの 1 枚の HTML にする。"""
    blocks, toc = _render(source, base or Path.cwd())

    # 目次は最初の見出しの直前に置く。目印（\x01）は 1 つ目だけ残して消す
    rendered: list[str] = []
    inserted = False
    for block in blocks:
        if block.startswith("\x01"):
            if not inserted:
                rendered.append(_toc_html(toc))
                inserted = True
            continue
        rendered.append(block)

    body = "\n".join(rendered)
    return (
        "<!doctype html>\n"
        '<html lang="ja"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>"
        f"<style>{_STYLE}</style></head><body>\n{body}\n</body></html>\n"
    )


# ----------------------------------------------------------------------
# PDF 化
# ----------------------------------------------------------------------
def find_edge() -> Path:
    """Microsoft Edge の実行ファイルを探す。"""
    for candidate in EDGE_CANDIDATES:
        if candidate.is_file():
            return candidate
    found = shutil.which("msedge")
    if found:
        return Path(found)
    raise ManualError(
        "Microsoft Edge が見つかりません。PDF 化には Edge を使います。\n"
        "見つからない場合は --html-only で HTML だけ作り、"
        "ブラウザの［印刷］→［PDF として保存］で PDF にしてください。"
    )


def html_to_pdf(html_path: Path, pdf_path: Path) -> None:
    """Edge のヘッドレスモードで PDF にする。

    出力先はいったん一時フォルダの ASCII 名にする。日本語を含むパスを
    そのまま渡すと、Edge 側の書き出しで失敗することがあるため。
    プロファイルも一時フォルダに切る（Edge を開いたままでも動くように）。
    """
    edge = find_edge()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="win_manual_") as temp:
        staged = Path(temp) / "out.pdf"
        command = [
            str(edge),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={Path(temp) / 'profile'}",
            "--no-pdf-header-footer",
            "--virtual-time-budget=5000",
            f"--print-to-pdf={staged}",
            html_path.as_uri(),
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if not staged.is_file():
            detail = (result.stderr or result.stdout or "").strip()
            raise ManualError(
                f"PDF を作れませんでした（終了コード {result.returncode}）。\n{detail}"
            )
        shutil.copyfile(staged, pdf_path)


def build(md_path: Path, pdf_path: Path, *, html_only: bool = False) -> Path:
    """Markdown から PDF を作る。作ったファイルの場所を返す。"""
    try:
        source = md_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManualError(f"{md_path} を読めませんでした: {exc}") from exc

    first = next((line for line in source.splitlines() if line.startswith("# ")), "")
    title = first[2:].strip() or md_path.stem

    html_path = pdf_path.with_suffix(".html")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(
        to_html(source, title, md_path.parent), encoding="utf-8"
    )

    if html_only:
        return html_path

    html_to_pdf(html_path, pdf_path)
    return pdf_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Markdown のマニュアルを PDF にする")
    parser.add_argument("markdown", help="もとにする .md ファイル")
    parser.add_argument("-o", "--output", help="出力先の .pdf（既定: build/docs/）")
    parser.add_argument(
        "--html-only", action="store_true", help="PDF にせず HTML だけ作る"
    )
    parser.add_argument("--open", action="store_true", help="作ったら開く")
    args = parser.parse_args()

    md_path = Path(args.markdown)
    if not md_path.is_absolute():
        md_path = (ROOT / md_path).resolve()
    if not md_path.is_file():
        print(f"エラー: {md_path} がありません。", file=sys.stderr)
        raise SystemExit(2)

    if args.output:
        pdf_path = Path(args.output)
        if not pdf_path.is_absolute():
            pdf_path = (ROOT / pdf_path).resolve()
    else:
        # 「win_rpa の MANUAL.md」→「win_rpa_マニュアル.pdf」
        stem = md_path.stem
        if stem.upper() == "MANUAL":
            stem = f"{md_path.parent.name}_マニュアル"
        pdf_path = DEFAULT_OUT_DIR / f"{stem}.pdf"

    try:
        made = build(md_path, pdf_path, html_only=args.html_only)
    except ManualError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        raise SystemExit(1)

    size = made.stat().st_size / 1024
    print(f"作りました: {made}（{size:,.0f} KB）")

    if args.open:
        os.startfile(made)   # noqa: S606 - 作ったファイルを開くだけ


if __name__ == "__main__":
    main()
