"""Draw a Thai page: the English page with its translated text replaced (mirror layout).

The original page is copied; only the English text that has a translation is
removed (boxes, table shading, code and images stay), and the Thai is drawn in
the same places. Thai needs word boundaries for line breaks (PyThaiNLP inserts
zero-width spaces) and proper shaping for stacked vowels and tone marks (MuPDF's
HTML layout uses HarfBuzz).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

FONT_DIR = Path(__file__).parent / "fonts"
RENDERER_VERSION = "1"
ZWSP = chr(0x200B)
THAI_RUN = re.compile("[" + chr(0x0E00) + "-" + chr(0x0E7F) + "]+")
TAG_HTML = {
    "<c>": "<code>", "</c>": "</code>", "<k>": '<span class="k">', "</k>": "</span>",
    "<b>": "<b>", "</b>": "</b>",
}  # fmt: skip
BULLETS = set("•·▪◦‣∙●○■□➢►▶✓✔❖◆◇–-")
BASE_CSS = """
@font-face {font-family: th; src: url(IBMPlexSansThai-Regular.ttf);}
@font-face {font-family: th; src: url(IBMPlexSansThai-Bold.ttf); font-weight: bold;}
@font-face {font-family: mono; src: url(IBMPlexMono-Medium.ttf);}
@font-face {font-family: mono; src: url(IBMPlexMono-Bold.ttf); font-weight: bold;}
* {font-family: th;}
body {margin: 0;}
code {font-family: mono; font-size: 0.95em;}
.k, .k code {color: #0072bc; font-weight: bold;}
"""


@dataclass
class Fit:
    block_id: int
    scale: float  # 1.0 = original size; smaller = shrunk to fit
    overflow: bool  # still did not fit at the smallest allowed size


def word_breaks(text: str) -> str:
    """Zero-width spaces between Thai words, so lines can break between them."""
    from pythainlp.tokenize import word_tokenize

    def split(m: re.Match) -> str:
        return ZWSP.join(w for w in word_tokenize(m.group(0), engine="newmm") if w)

    return THAI_RUN.sub(split, text)


def to_html(markup_th: str) -> str:
    out, in_code = [], 0
    for part in re.split(r"(</?[ckb]>)", markup_th or ""):
        if part in TAG_HTML:
            in_code += {"<c>": 1, "</c>": -1}.get(part, 0)
            out.append(TAG_HTML[part])
        elif part:
            text = html.escape(html.unescape(part), quote=False)
            out.append(text if in_code else word_breaks(text))
    return "".join(out).replace("\n", "<br>")


def _marker_prefix(marker: str | None) -> str:
    marker = (marker or "").strip()
    if not marker:
        return ""
    return "• " if marker in BULLETS else f"{html.escape(marker, quote=False)} "


def render_thai_page(pdf_path: str | Path, page_no: int, blocks: list[dict]):
    """Returns (one-page pymupdf.Document, list[Fit]). ``blocks`` are rows of the
    blocks table for this page (x0..y1, role, style, markup_th, cells_th, translate)."""
    import pymupdf

    from pdftr.ingest.pdf_spans import fill_boxes

    src = pymupdf.open(str(pdf_path))
    doc = pymupdf.open()
    doc.insert_pdf(src, from_page=page_no - 1, to_page=page_no - 1)
    page = doc[0]
    width, height = page.rect.width, page.rect.height
    fills = [pymupdf.Rect(*fb) for fb, _rgb in fill_boxes(page)]
    rects = {b["id"]: pymupdf.Rect(b["x0"], b["y0"], b["x1"], b["y1"]) for b in blocks}
    columns = [rects[b["id"]] for b in blocks if b["role"] == "paragraph"]

    jobs = []  # (block id, redact rects, target rect, html, css)
    for b in blocks:
        rect = rects[b["id"]]
        style = b.get("style") or {}
        if b["role"] == "table" and b.get("cells_th"):
            jobs.extend(_table_jobs(page, b, b["cells_th"]))
            continue
        if not b.get("translate") or not b.get("markup_th"):
            continue
        size = float(style.get("size") or 10)
        pitch = style.get("pitch")
        line_height = max((pitch / size) if pitch else 1.45, 1.42)
        container = next((f for f in fills if f.contains(rect) and f.width < width * 0.9), None)
        others = [r for i, r in rects.items() if i != b["id"]] + fills
        x1 = rect.x1 + 2
        if b["role"] != "header":  # short lines may widen into free space on their right
            x1 = max(x1, _free_right(rect, others, container, columns, width))
        below = [  # anything under the (widened) block limits how far down it may grow
            o.y0
            for o in others
            if o.y0 >= rect.y1 - 1 and o.x0 < x1 and o.x1 > rect.x0 and not o.contains(rect)
        ]
        y1 = min(below) - 2 if below else min(height - 24, rect.y1 + 60)
        if container is not None:
            y1 = min(y1, container.y1 - 3)
        target = pymupdf.Rect(rect.x0, rect.y0 - 1, x1, max(y1, rect.y1 + 1))
        align = "right" if b["role"] == "header" and rect.x0 > width / 2 else "left"
        prefix, hang = "", 0
        if b["role"] == "list_item":
            prefix = _marker_prefix(style.get("marker"))
            hang = 10 if prefix == "• " else 14 if prefix else 0
        weight = " font-weight: bold;" if style.get("bold") or b["role"] == "heading" else ""
        css = BASE_CSS + (
            f"body {{font-size: {size}pt; line-height: {line_height:.2f}; "
            f"color: {style.get('color') or '#231f20'}; text-align: {align};{weight} "
            f"padding-left: {hang}pt; text-indent: {style.get('indent', 0) - hang}pt;}}"
        )
        redact = [_inset(pymupdf.Rect(*r)) for r in style.get("lines") or []]
        jobs.append((b["id"], redact, target, prefix + to_html(b["markup_th"]), css))

    for _bid, redact, *_rest in jobs:
        for r in redact:
            page.add_redact_annot(r, fill=False)
    if jobs:
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE
        )
    archive = pymupdf.Archive(str(FONT_DIR))
    fits = []
    for bid, _redact, target, body, css in jobs:
        spare, scale = page.insert_htmlbox(target, body, css=css, archive=archive, scale_low=0.6)
        if spare < 0:  # nothing was drawn: draw it at whatever size fits, never lose text
            page.insert_htmlbox(target, body, css=css, archive=archive, scale_low=0)
        fits.append(Fit(bid, round(scale, 2), spare < 0))
    return doc, fits


def _free_right(rect, others, container, columns, page_width: float) -> float:
    """How far a block may grow to the right: to the right edge of its text column
    (the paragraphs it lines up with), its shaded box, or the next block on its lines."""
    limit = max(
        (c.x1 for c in columns if c.x0 <= rect.x0 + 6 and c.x1 > rect.x1),
        default=rect.x1 + 2,
    )
    if container is not None:
        limit = min(limit, container.x1 - 3)
    else:
        limit = min(limit, page_width - 24)
    for o in others:
        if o.x0 >= rect.x1 - 1 and o.y0 < rect.y1 and o.y1 > rect.y0:
            limit = min(limit, o.x0 - 4)
    return limit


def _inset(r, frac: float = 0.25):
    import pymupdf

    h = r.height * frac
    return pymupdf.Rect(r.x0 + 0.3, r.y0 + h, r.x1 - 0.3, r.y1 - h)


def _table_jobs(page, block: dict, cells_th: dict) -> list:
    import pymupdf

    jobs = []
    for cell in (block.get("style") or {}).get("cells") or []:
        thai = cells_th.get(f"{cell['row']},{cell['col']}")
        if not thai or not cell.get("bbox"):
            continue
        crect = pymupdf.Rect(*cell["bbox"])
        data = page.get_text("dict", clip=crect)
        lines = [
            ln
            for b in data["blocks"]
            if b["type"] == 0
            for ln in b["lines"]
            if any(s["text"].strip() for s in ln["spans"])
        ]
        if not lines:
            continue
        spans = [s for ln in lines for s in ln["spans"] if s["text"].strip()]
        size = max(spans, key=lambda s: len(s["text"]))["size"]
        color = f"#{max(spans, key=lambda s: len(s['text']))['color']:06x}"
        first = pymupdf.Rect(lines[0]["bbox"])
        target = pymupdf.Rect(first.x0, first.y0 - 1.5, crect.x1 - 3, crect.y1 - 1)
        weight = " font-weight: bold;" if cell.get("header") else ""
        css = BASE_CSS + (
            f"body {{font-size: {size:.1f}pt; line-height: 1.35; color: {color};{weight}}}"
        )
        redact = [_inset(pymupdf.Rect(ln["bbox"])) for ln in lines]
        jobs.append((block["id"], redact, target, to_html(html.escape(thai, quote=False)), css))
    return jobs
