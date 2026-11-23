"""Contents pages, read from the PDF's own text lines.

Docling's cells are unreliable here (two entries in one cell, one title split over two
cells), so a contents block is re-read line by line: an entry is a title, possibly
wrapped onto a second line, plus the page number on its baseline. Translation and
rendering both call ``entries`` on the same page, so they agree on what an entry is.
The Thai of an entry is stored in the block's ``cells_th`` under ``key(title)``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import pairwise

from pdftr.translate.titles import norm_title

Rect = tuple[float, float, float, float]
NUMBER_RE = re.compile(r"^(?:\d{1,4}|[ivxlcdm]{1,7})$")  # roman page numbers are lowercase
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
LEADERS = set("\x08\t.·…_ ")
STRONG_LEADERS = set("\x08\t.·…")


@dataclass
class Entry:
    title: str  # English title, cleaned
    rects: list[Rect] = field(default_factory=list)  # one per title line (removed in Thai)
    number: Rect | None = None  # the page number (kept)
    right: float = 0.0  # how far the Thai may extend
    size: float = 10.0
    color: int = 0
    bold: bool = False


def key(title: str) -> str:
    return "t:" + norm_title(clean(title))


def is_contents(block: dict) -> bool:
    """A contents block: Docling's "toc", or a table on a contents page."""
    return block["role"] == "toc" or (block["role"] == "table" and block.get("page_kind") == "toc")


def exclusions(blocks: list[dict]) -> list[Rect]:
    """Areas of a page's headings and page headers, which have their own Thai."""
    return [
        (b["x0"], b["y0"], b["x1"], b["y1"])
        for b in blocks
        if b["role"] in ("heading", "header") and b.get("translate", True)
    ]


def clean(text: str) -> str:
    return " ".join(CONTROL_RE.sub(" ", text).split())


def _visible(c: dict) -> bool:
    return bool(c["c"].strip()) and not CONTROL_RE.match(c["c"])


def _bbox(chars: list[dict]) -> Rect:
    xs0, ys0, xs1, ys1 = zip(*(c["bbox"] for c in chars), strict=True)
    return (min(xs0), min(ys0), max(xs1), max(ys1))


@dataclass
class _Line:
    chars: list[dict]
    size: float
    color: int
    bold: bool

    @property
    def rect(self) -> Rect:
        return _bbox([c for c in self.chars if _visible(c)])

    @property
    def text(self) -> str:
        return "".join(c["c"] for c in self.chars)


def _lines(page, rect: Rect) -> list[_Line]:
    import pymupdf

    out = []
    for b in page.get_text("rawdict", clip=pymupdf.Rect(rect))["blocks"]:
        for ln in b.get("lines", []):
            chars, style = [], None
            for s in ln["spans"]:
                chars.extend(s["chars"])
                if style is None and any(_visible(c) for c in s["chars"]):
                    style = (s["size"], s["color"], bool(s["flags"] & 16) or "Bold" in s["font"])
            if style is not None:
                out.append(_Line(chars, *style))
    return out


def _trim(chars: list[dict]) -> list[dict]:
    end = len(chars)
    while end and (chars[end - 1]["c"] in LEADERS or CONTROL_RE.match(chars[end - 1]["c"])):
        end -= 1
    return chars[:end]


def _split_number(line: _Line) -> tuple[list[dict], list[dict]]:
    """Title chars and page-number chars of one line. A trailing number counts as a page
    number only when set apart (a leader or a wide gap): "Chapter 11" stays a title."""
    chars = _trim(line.chars)
    start = len(chars)
    while start and chars[start - 1]["c"].isalnum():
        start -= 1
    number = "".join(c["c"] for c in chars[start:])
    if start == 0 or not NUMBER_RE.match(number):
        return chars, []
    lead = start
    while lead and (chars[lead - 1]["c"] in LEADERS or CONTROL_RE.match(chars[lead - 1]["c"])):
        lead -= 1
    between = {c["c"] for c in chars[lead:start]}
    gap = chars[start]["bbox"][0] - chars[lead - 1]["bbox"][2] if lead else 0.0
    if between & STRONG_LEADERS or gap > 3 * line.size:
        return _trim(chars[:lead]), chars[start:]
    return chars, []


def entries(page, rect: Rect, exclude: list[Rect] = ()) -> list[Entry]:
    """The contents entries inside ``rect`` (x0, y0, x1, y1) of ``page``. Lines inside
    ``exclude`` (headings and page headers translated on their own) are left out."""
    numbers, titles = [], []
    for ln in _lines(page, rect):
        r = ln.rect
        cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
        if any(e[0] <= cx <= e[2] and e[1] <= cy <= e[3] for e in exclude):
            continue
        (numbers if NUMBER_RE.match(clean(ln.text)) else titles).append(ln)
    column = _columns([t.rect[0] for t in titles], (rect[2] - rect[0]) / 4)
    titles.sort(key=lambda t: (column(t.rect[0]), t.rect[1]))  # column, then top to bottom
    used: set[int] = set()
    result: list[Entry] = []
    pending: tuple[_Line, list[dict]] | None = None  # a title line without a page number

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            result.append(_entry([pending[1]], pending[0], None))
            pending = None

    for ln in titles:
        title_chars, number_chars = _split_number(ln)
        number = _bbox(number_chars) if number_chars else _number_on_line(ln, numbers, used, column)
        if pending is not None:
            prev, prev_chars = pending
            pr, tr = prev.rect, ln.rect
            same_style = (prev.size, prev.color, prev.bold) == (ln.size, ln.color, ln.bold)
            wraps = abs(pr[0] - tr[0]) < 3 and -1 <= tr[1] - pr[3] < 0.8 * (tr[3] - tr[1])
            if same_style and wraps:  # the title continues on this line
                result.append(_entry([prev_chars, title_chars], prev, number))
                pending = None
                continue
            flush()
        if number is None:
            pending = (ln, title_chars)
        else:
            result.append(_entry([title_chars], ln, number))
    flush()
    for e in result:
        widest = max(r[2] for r in e.rects)
        e.right = max(widest, e.number[0] - 6 if e.number else rect[2] - 2)
    return [e for e in result if e.title and e.rects]


def _columns(starts: list[float], jump: float):
    """Column number of an x position. Columns begin where title starts jump by more than
    ``jump`` (so a single column with right-aligned page numbers stays one column)."""
    xs = sorted(starts)
    bounds = [b for a, b in pairwise(xs) if b - a > jump]
    return lambda x: sum(1 for b in bounds if x >= b - 1)


def _number_on_line(ln: _Line, numbers: list[_Line], used: set[int], column) -> Rect | None:
    """A page number printed as its own line on the title's baseline: the nearest one to
    the right, in the same column."""
    tr = ln.rect
    best, best_gap = None, None
    for i, n in enumerate(numbers):
        nr = n.rect
        overlap = min(nr[3], tr[3]) - max(nr[1], tr[1])
        same_column = column(nr[0]) == column(tr[0])
        gap = nr[0] - tr[2]
        if i in used or not same_column or gap < 4 or overlap <= 0.5 * (tr[3] - tr[1]):
            continue
        if best_gap is None or gap < best_gap:
            best, best_gap = i, gap
    if best is None:
        return None
    used.add(best)
    return numbers[best].rect


def _entry(parts: list[list[dict]], first: _Line, number: Rect | None) -> Entry:
    rects, texts = [], []
    for chars in parts:
        chars = _trim(chars)
        visible = [c for c in chars if _visible(c)]
        if visible:
            rects.append(_bbox(visible))
            texts.append("".join(c["c"] for c in chars))
    return Entry(
        title=clean(" ".join(texts)),
        rects=rects,
        number=number,
        size=first.size,
        color=first.color,
        bold=first.bold,
    )
