"""Exact text geometry and styling from the PDF (PyMuPDF)."""

from __future__ import annotations

import html

import pymupdf

from pdftr.ingest.models import Line, Span
from pdftr.ingest.textutil import clean_text

MONO_HINTS = (
    "mono",
    "courier",
    "consol",
    "code",
    "menlo",
    "inconsolata",
    "lucidaconsole",
    "typewriter",
)
BOLD_HINTS = ("bold", "black", "heavy", "semibold", "demibold", "extrab")
FLAG_MONO = 8
FLAG_BOLD = 16


def _is_mono(font: str, flags: int) -> bool:
    f = font.lower().replace(" ", "").replace("-", "")
    return bool(flags & FLAG_MONO) or any(h in f for h in MONO_HINTS)


def _is_bold(font: str, flags: int) -> bool:
    f = font.lower().replace(" ", "").replace("-", "")
    return bool(flags & FLAG_BOLD) or any(h in f for h in BOLD_HINTS)


def is_colored(color: int) -> bool:
    """True for saturated colors (blue key terms), False for black, gray, white."""
    r, g, b = (color >> 16) & 255, (color >> 8) & 255, color & 255
    return max(r, g, b) - min(r, g, b) > 40


def page_lines(page: pymupdf.Page) -> list[Line]:
    """All horizontal text lines on the page, with styled spans."""
    flags = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP
    data = page.get_text("dict", flags=flags)
    lines: list[Line] = []
    for block in data["blocks"]:
        if block["type"] != 0:
            continue
        for ln in block["lines"]:
            dx, dy = ln["dir"]
            if abs(dy) > 0.1 or dx < 0:  # rotated or vertical text (side tabs)
                continue
            spans = []
            for s in ln["spans"]:
                text = s["text"]
                if not text:
                    continue
                spans.append(
                    Span(
                        text=text.replace("\u00ad", ""),
                        bbox=tuple(s["bbox"]),
                        font=s["font"],
                        size=float(s["size"]),
                        color=int(s["color"]),
                        bold=_is_bold(s["font"], s["flags"]),
                        mono=_is_mono(s["font"], s["flags"]),
                    )
                )
            if any(clean_text(s.text) for s in spans):
                lines.append(Line(bbox=tuple(ln["bbox"]), spans=spans))
    return lines


def fill_boxes(page: pymupdf.Page, min_w: float = 40, min_h: float = 12) -> list[tuple]:
    """Non-white filled rectangles (note boxes, shaded panels): (bbox, rgb)."""
    boxes = []
    pr = page.rect
    for d in page.get_drawings():
        fill = d.get("fill")
        if fill is None:
            continue
        r = d["rect"]
        if r.width < min_w or r.height < min_h:
            continue
        if r.width > pr.width * 0.95 and r.height > pr.height * 0.9:
            continue  # page background
        if len(fill) == 1:
            rgb = (fill[0],) * 3
        elif len(fill) == 4:  # CMYK
            c, m, y, k = fill
            rgb = ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
        else:
            rgb = tuple(fill[:3])
        lum = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
        if lum > 0.97:
            continue
        boxes.append(((r.x0, r.y0, r.x1, r.y1), rgb))
    return boxes


def line_markup(line: Line, allow_key: bool = True) -> tuple[str, bool, bool]:
    """Markup for one line: <c> code, <k> colored bold key term, <b> bold.

    Returns (markup, starts_with_code, ends_with_code).
    """
    runs: list[tuple[tuple[bool, bool, bool], str]] = []
    for s in line.spans:
        key = allow_key and s.bold and is_colored(s.color)
        attrs = (key, s.mono, s.bold and not key and not s.mono)
        if not s.text.strip() and runs:
            # whitespace-only span: attach to the previous run
            runs[-1] = (runs[-1][0], runs[-1][1] + s.text)
            continue
        if runs and runs[-1][0] == attrs:
            runs[-1] = (attrs, runs[-1][1] + s.text)
        else:
            runs.append((attrs, s.text))
    parts = []
    for (key, code, bold), text in runs:
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()) :]
        # escape so book text like "<b>" or "a < b" never looks like our tags
        core = html.escape(text.strip(), quote=False)
        if not core:
            parts.append(text)
            continue
        if code:
            core = f"<c>{core}</c>"
        if key:
            core = f"<k>{core}</k>"
        elif bold:
            core = f"<b>{core}</b>"
        parts.append(f"{lead}{core}{trail}")
    markup = clean_text("".join(parts))
    starts_code = bool(runs) and runs[0][0][1]
    ends_code = bool(runs) and runs[-1][0][1]
    return markup, starts_code, ends_code
