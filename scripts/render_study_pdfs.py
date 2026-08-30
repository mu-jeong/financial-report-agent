"""Render docs/study/*.md into formatted PDFs.

Uses PyMuPDF's Story HTML engine (no external markdown/pandoc dependency).
- Converts SVG diagrams to PNG first (fitz rasterizes SVG with system fonts).
- Converts Markdown -> minimal HTML -> PDF, embedding the PNG diagrams.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import fitz

STUDY_DIR = Path(__file__).resolve().parent.parent / "docs" / "study"
DIAGRAMS_DIR = STUDY_DIR / "diagrams"

PAGE = fitz.Rect(0, 0, 595, 842)  # A4 portrait
BODY = fitz.Rect(48, 48, 547, 794)

CSS = """
body { font-family: sans-serif; font-size: 10.5pt; line-height: 1.55; color: #1f2937; }
h1 { font-size: 21pt; border-bottom: 2px solid #6366f1; padding-bottom: 6pt; margin-top: 4pt; color: #1e293b; }
h2 { font-size: 15pt; margin-top: 20pt; color: #1e293b; border-bottom: 1px solid #e2e8f0; padding-bottom: 3pt; }
h3 { font-size: 12.5pt; margin-top: 16pt; color: #334155; }
h4 { font-size: 11pt; margin-top: 12pt; color: #475569; }
p { margin: 6pt 0; }
pre { background-color: #f6f8fa; border: 1px solid #d0d7de; padding: 8pt; font-size: 8.8pt; line-height: 1.4; white-space: pre-wrap; }
code { background-color: #f6f8fa; padding: 1pt 3pt; font-size: 9.3pt; }
pre code { background-color: transparent; padding: 0; }
table { border-collapse: collapse; width: 100%; font-size: 8.8pt; margin: 8pt 0; }
th, td { border: 1px solid #d1d5db; padding: 4pt 6pt; text-align: left; }
th { background-color: #f3f4f6; font-weight: bold; }
blockquote { border-left: 3px solid #6366f1; margin: 8pt 0; padding: 2pt 10pt; color: #475569; background-color: #f8fafc; }
ul, ol { margin: 6pt 0; padding-left: 22pt; }
li { margin: 2pt 0; }
hr { border: none; border-top: 1px solid #e2e8f0; margin: 14pt 0; }
img { max-width: 100%; height: auto; }
a { color: #4f46e5; text-decoration: none; }
"""


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _inline(text: str) -> str:
    # inline code
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # bold
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    # links [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def markdown_to_html(md: str) -> str:
    lines = md.split("\n")
    html: list[str] = []
    i = 0
    in_code = False
    code_buf: list[str] = []
    in_para: list[str] = []
    in_list: str | None = None  # "ul" or "ol"

    def flush_para():
        nonlocal in_para
        if in_para:
            html.append("<p>" + _inline(" ".join(in_para)) + "</p>")
            in_para = []

    def flush_list():
        nonlocal in_list
        if in_list:
            html.append(f"</{in_list}>")
            in_list = None

    while i < len(lines):
        line = lines[i]

        # fenced code block
        if line.strip().startswith("```"):
            if not in_code:
                flush_para(); flush_list()
                in_code = True; code_buf = []
            else:
                lang = ""
                html.append("<pre><code>" + _escape_html("\n".join(code_buf)) + "</code></pre>")
                in_code = False; code_buf = []
            i += 1
            continue

        if in_code:
            code_buf.append(line)
            i += 1
            continue

        stripped = line.strip()

        # blank line
        if not stripped:
            flush_para(); flush_list()
            i += 1
            continue

        # horizontal rule
        if re.fullmatch(r"[-*_]{3,}", stripped):
            flush_para(); flush_list()
            html.append("<hr/>")
            i += 1
            continue

        # headings
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            flush_para(); flush_list()
            level = len(m.group(1))
            html.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # image
        m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)$", stripped)
        if m:
            flush_para(); flush_list()
            alt = _escape_html(m.group(1))
            src = m.group(2)
            html.append(f'<img src="{src}" alt="{alt}"/>')
            i += 1
            continue

        # blockquote
        if stripped.startswith(">"):
            flush_para(); flush_list()
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip()[1:].strip())
                i += 1
            html.append("<blockquote>" + _inline(" ".join(quote_lines)) + "</blockquote>")
            continue

        # table
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:\-|]+\|$", lines[i + 1].strip()):
            flush_para(); flush_list()
            header = [c.strip() for c in stripped.strip("|").split("|")]
            i += 2  # skip header + separator
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            html.append("<table><thead><tr>" + "".join(f"<th>{_inline(h)}</th>" for h in header) + "</tr></thead><tbody>")
            for row in rows:
                html.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>")
            html.append("</tbody></table>")
            continue

        # unordered list
        m = re.match(r"^[-*]\s+(.*)$", stripped)
        if m:
            flush_para()
            if in_list != "ul":
                flush_list(); html.append("<ul>"); in_list = "ul"
            html.append("<li>" + _inline(m.group(1)) + "</li>")
            i += 1
            continue

        # ordered list
        m = re.match(r"^\d+\.\s+(.*)$", stripped)
        if m:
            flush_para()
            if in_list != "ol":
                flush_list(); html.append("<ol>"); in_list = "ol"
            html.append("<li>" + _inline(m.group(1)) + "</li>")
            i += 1
            continue

        # paragraph
        flush_list()
        in_para.append(stripped)
        i += 1

    flush_para(); flush_list()
    if in_code:
        html.append("<pre><code>" + _escape_html("\n".join(code_buf)) + "</code></pre>")

    return "\n".join(html)


def svg_to_png(svg_path: Path, png_path: Path, *, zoom: float = 2.2) -> None:
    doc = fitz.open(str(svg_path))
    page = doc[0]
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    pix.save(str(png_path))
    doc.close()


def render_md_to_pdf(md_path: Path, pdf_path: Path) -> None:
    md = md_path.read_text(encoding="utf-8")
    html = markdown_to_html(md)
    story = fitz.Story(html=html, user_css=CSS, archive=fitz.Archive(str(STUDY_DIR)))

    def rectfn(rect_num, filled):
        return PAGE, BODY, None

    doc = story.write_with_links(rectfn)
    doc.save(str(pdf_path), garbage=4, deflate=True, clean=True)
    doc.close()


def main() -> None:
    DIAGRAMS_DIR.mkdir(parents=True, exist_ok=True)

    # 1) SVG -> PNG
    for svg in sorted(DIAGRAMS_DIR.glob("*.svg")):
        png = svg.with_suffix(".png")
        svg_to_png(svg, png)
        print(f"[diagram] {svg.name} -> {png.name} ({png.stat().st_size} bytes)")

    # 2) Markdown -> PDF
    for md in sorted(STUDY_DIR.glob("*.md")):
        pdf = md.with_suffix(".pdf")
        render_md_to_pdf(md, pdf)
        doc = fitz.open(str(pdf))
        first = doc[0].get_text()[:120]
        n = doc.page_count
        doc.close()
        print(f"[pdf] {md.name} -> {pdf.name} ({n} pages) first={first[:40]!r}")


if __name__ == "__main__":
    main()
