"""Merge Docling layout items with PyMuPDF lines into blocks, then refine roles."""

from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict

from pdftr.ingest.models import BBox, Block, DoclingItem, Line
from pdftr.ingest.pdf_spans import line_markup
from pdftr.ingest.textutil import clean_text, join_lines, strip_markup

ROLE_BY_LABEL = {
    "section_header": "heading",
    "title": "heading",
    "text": "paragraph",
    "paragraph": "paragraph",
    "reference": "paragraph",
    "list_item": "list_item",
    "caption": "caption",
    "footnote": "footnote",
    "code": "code",
    "table": "table",
    "document_index": "toc",
    "picture": "picture",
    "chart": "picture",
    "formula": "formula",
    "page_header": "header",
    "page_footer": "footer",
}
CALLOUT_RE = re.compile(
    r"^(?:<b>)?(Note|Tip|Caution|Warning|Important)\s*\d*\s*(?:</b>)?\s*(?:<k>)?[|:]", re.IGNORECASE
)
LABEL_VALUE_RE = re.compile(r"^[A-Z][A-Za-z ]{1,30}:\s+\S")
MARKER_RE = re.compile(
    r"^\s*(?:<[ckb]>)?\s*(?:[•·▪◦‣∙●○■□➢►▶✓✔❖◆◇–-]|\(?\d{1,3}[.)]|\(?[A-Za-z][.)])(?:\s*</[ckb]>)?\s+"
)


def _area(b: BBox) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _contains(outer: BBox, x: float, y: float, pad: float = 2.0) -> bool:
    return outer[0] - pad <= x <= outer[2] + pad and outer[1] - pad <= y <= outer[3] + pad


def _inside(inner: BBox, outer: BBox, pad: float = 3.0) -> bool:
    return (
        inner[0] >= outer[0] - pad
        and inner[1] >= outer[1] - pad
        and inner[2] <= outer[2] + pad
        and inner[3] <= outer[3] + pad
    )


def _union(boxes: list[BBox]) -> BBox:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def assign_lines(
    items: list[DoclingItem], lines: list[Line]
) -> tuple[dict[int, list[Line]], list[Line]]:
    """Give every PDF line to the smallest Docling region containing its center."""
    owned: dict[int, list[Line]] = defaultdict(list)
    orphans: list[Line] = []
    for line in lines:
        cx = (line.bbox[0] + line.bbox[2]) / 2
        cy = (line.bbox[1] + line.bbox[3]) / 2
        hits = [i for i, it in enumerate(items) if _contains(it.bbox, cx, cy)]
        if not hits:
            orphans.append(line)
            continue
        best = min(hits, key=lambda i: _area(items[i].bbox))
        owned[best].append(line)
    for i in owned:
        owned[i] = sort_lines(owned[i])
    return owned, orphans


def sort_lines(lines: list[Line]) -> list[Line]:
    """Rows top to bottom, then left to right within a row.

    Lines whose vertical extents overlap by at least half the smaller height
    share a row: a bullet glyph sits slightly lower than its text, so sorting
    by top edge alone would put it after the first text line.
    """
    rows: list[list[Line]] = []
    for ln in sorted(lines, key=lambda x: (x.bbox[1] + x.bbox[3]) / 2):
        if rows:
            top = min(x.bbox[1] for x in rows[-1])
            bottom = max(x.bbox[3] for x in rows[-1])
            overlap = min(bottom, ln.bbox[3]) - max(top, ln.bbox[1])
            smaller = min(bottom - top, ln.bbox[3] - ln.bbox[1])
            if smaller > 0 and overlap >= 0.5 * smaller:
                rows[-1].append(ln)
                continue
        rows.append([ln])
    return [ln for row in rows for ln in sorted(row, key=lambda x: x.bbox[0])]


def _style(lines: list[Line], bbox: BBox) -> dict:
    sizes: Counter = Counter()
    colors: Counter = Counter()
    bold_chars = total = 0
    for ln in lines:
        for s in ln.spans:
            n = len(s.text.strip())
            sizes[round(s.size, 1)] += n
            colors[s.color] += n
            total += n
            bold_chars += n if s.bold else 0
    rects = [list(map(lambda v: round(v, 1), ln.bbox)) for ln in lines]
    pitch = None
    if len(lines) > 1:
        pitch = round((lines[-1].bbox[1] - lines[0].bbox[1]) / (len(lines) - 1), 2)
    return {
        "size": sizes.most_common(1)[0][0] if sizes else None,
        "color": f"#{colors.most_common(1)[0][0]:06x}" if colors else None,
        "bold": total > 0 and bold_chars / total > 0.6,
        "indent": round(lines[0].bbox[0] - bbox[0], 1) if lines else 0.0,
        "pitch": pitch,
        "lines": rects,
    }


def build_page_blocks(
    page_no: int,
    page_size: tuple[float, float],
    items: list[DoclingItem],
    lines: list[Line],
    fills: list[tuple],
    vocab: set[str],
) -> list[Block]:
    items = _drop_false_tables(sorted(items, key=lambda it: it.order))
    owned, orphans = assign_lines(items, lines)
    page_w, page_h = page_size
    blocks: list[Block] = []

    for idx, item in enumerate(items):
        role = ROLE_BY_LABEL.get(item.label, "other")
        if item.layer == "furniture" and role not in ("header", "footer"):
            role = "header" if item.bbox[1] < page_h / 2 else "footer"
        its_lines = owned.get(idx, [])
        bbox = _union([item.bbox] + [ln.bbox for ln in its_lines]) if its_lines else item.bbox
        flags: list[str] = []
        if item.multi_prov:
            flags.append("multi_region")

        if role in ("table", "toc"):
            markup = ""
            text = clean_text(item.table_md or item.text)
        elif role == "code":
            text = "\n".join(ln.text.rstrip() for ln in its_lines) or item.text
            markup = ""
        elif role in ("picture", "formula"):
            text = clean_text(item.text)
            markup = ""
        else:
            allow_key = role not in ("heading", "header", "footer", "caption")
            parts = [line_markup(ln, allow_key=allow_key) for ln in its_lines]
            markup = join_lines(parts, vocab) if parts else clean_text(item.text)
            if role == "list_item":
                markup = MARKER_RE.sub("", markup, count=1)
            text = clean_text(strip_markup(markup))
            if its_lines and item.text:
                ratio = difflib.SequenceMatcher(
                    None, text[:300], clean_text(item.text)[:300]
                ).ratio()
                if ratio < 0.7:
                    flags.append("text_mismatch")

        if role == "heading" and LABEL_VALUE_RE.match(text):
            role = "paragraph"  # "Estimated Time: 50 minutes" is a field, not a section
            flags.append("demoted_heading")

        if role == "paragraph":
            in_box = any(_inside(bbox, fb) and fb[2] - fb[0] < page_w * 0.9 for fb, _ in fills)
            if CALLOUT_RE.match(markup) or (in_box and not text.endswith("?")):
                role = "callout"

        blocks.append(
            Block(
                page_no=page_no,
                seq=len(blocks) + 1,
                role=role,
                docling_label=item.label,
                bbox=tuple(round(v, 2) for v in bbox),
                markup=markup,
                text=text,
                marker=item.marker,
                table_md=item.table_md,
                style=_style(its_lines, bbox)
                | ({"cells": item.table_cells} if item.table_cells else {}),
                flags=flags,
            )
        )

    # Lines Docling did not cover (often the first lines of a page that continue a
    # paragraph from the previous page): keep them as paragraphs, flagged for review.
    for group in _group_orphans(orphans):
        markup = join_lines([line_markup(ln) for ln in group], vocab)
        text = clean_text(strip_markup(markup))
        if not text:
            continue
        bbox = _union([ln.bbox for ln in group])
        blocks.append(
            Block(
                page_no=page_no,
                seq=0,
                role="paragraph",
                docling_label="orphan",
                bbox=tuple(round(v, 2) for v in bbox),
                markup=markup,
                text=text,
                style=_style(group, bbox),
                flags=["orphan"],
            )
        )
    ordered = _reading_order(blocks)
    for seq, b in enumerate(ordered, 1):
        b.seq = seq
    return ordered


def _overlap(a: BBox, b: BBox) -> float:
    """Intersection area of a and b divided by the area of a."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return (w * h) / _area(a) if w > 0 and h > 0 and _area(a) > 0 else 0.0


def _drop_false_tables(items: list[DoclingItem]) -> list[DoclingItem]:
    """Drop one-column 'tables' that duplicate a text region (e.g. term lists)."""
    texts = [it for it in items if it.label in ("text", "paragraph", "list_item")]
    kept = []
    for it in items:
        if it.label == "table" and it.table_cells is not None:
            ncols = max((c["col"] + (c["col_span"] or 1) for c in it.table_cells), default=0)
            if ncols <= 1 and any(_overlap(it.bbox, t.bbox) > 0.6 for t in texts):
                continue
        kept.append(it)
    return kept


def _reading_order(blocks: list[Block]) -> list[Block]:
    """Docling's order, corrected so that headings divide the page into bands.

    Everything entirely above a heading is read before it (Docling sometimes
    reads a two-column list's right column after the next full-width heading).
    Orphan lines are slotted in by position inside their band.
    """
    headers = [b for b in blocks if b.role == "header"]
    footers = [b for b in blocks if b.role == "footer"]
    body = [b for b in blocks if b.role not in ("header", "footer")]
    heading_tops = sorted(b.bbox[1] for b in body if b.role == "heading")

    def band(b: Block) -> int:
        return sum(1 for top in heading_tops if top <= b.bbox[1] + 0.5)

    placed = sorted((b for b in body if "orphan" not in b.flags), key=band)  # stable
    for orphan in (b for b in body if "orphan" in b.flags):
        ob = band(orphan)
        index = next(
            (
                i
                for i, b in enumerate(placed)
                if band(b) == ob
                and b.bbox[1] >= orphan.bbox[1] - 2
                and min(b.bbox[2], orphan.bbox[2]) > max(b.bbox[0], orphan.bbox[0])
            ),
            None,
        )
        if index is None:
            index = max((i + 1 for i, b in enumerate(placed) if band(b) <= ob), default=0)
        placed.insert(index, orphan)
    return headers + placed + footers


def _group_orphans(lines: list[Line], gap: float = 4.0) -> list[list[Line]]:
    groups: list[list[Line]] = []
    for ln in sort_lines(lines):
        if groups and ln.bbox[1] - max(x.bbox[3] for x in groups[-1]) < gap:
            groups[-1].append(ln)
        else:
            groups.append([ln])
    return groups


# ---------------------------------------------------------------- page-level passes


def mark_boilerplate(blocks: list[Block], page_heights: dict[int, float], n_pages: int) -> None:
    """Text repeated near the top/bottom of many pages is page furniture."""

    def key(b: Block) -> str:
        return re.sub(r"\d+", "#", b.text.lower())[:120]

    edge = [b for b in blocks if b.text and _near_edge(b, page_heights[b.page_no])]
    counts = Counter(key(b) for b in edge)
    threshold = max(3, int(0.3 * n_pages))
    for b in edge:
        small = (b.style.get("size") or 99) <= 5.5
        if counts[key(b)] >= threshold or small:
            if b.role not in ("header", "footer"):
                b.flags.append("boilerplate")
            b.role = "header" if b.bbox[1] < page_heights[b.page_no] / 2 else "footer"


def _near_edge(b: Block, page_h: float) -> bool:
    return b.bbox[3] < page_h * 0.09 or b.bbox[1] > page_h * 0.92


def _norm_title(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def assign_heading_levels(blocks: list[Block], outline: list[list]) -> None:
    """Heading level = outline (bookmark) level when matched, else by font size."""
    entries = [(lvl, title, page, _norm_title(title)) for lvl, title, page, *_ in outline]
    matched_sizes: dict[int, list[float]] = defaultdict(list)
    unmatched: list[Block] = []
    headings = [b for b in blocks if b.role == "heading"]
    for b in headings:
        nt = _norm_title(b.text)
        best, best_score = None, 0.0
        for lvl, title, page, ntitle in entries:
            if abs(page - b.page_no) > 1 or not nt:
                continue
            if nt == ntitle or (len(nt) > 3 and (nt in ntitle or ntitle in nt)):
                score = 1.0
            else:
                score = difflib.SequenceMatcher(None, nt, ntitle).ratio()
            if score > best_score:
                best, best_score = (lvl, title), score
        if best and best_score >= 0.85:
            b.level = best[0]
            b.style["outline_title"] = best[1]
            if b.style.get("size"):
                matched_sizes[best[0]].append(b.style["size"])
        else:
            unmatched.append(b)
    top = max(matched_sizes, default=0)
    floor_size = min(matched_sizes[top]) if top else None
    sizes = sorted({round(b.style.get("size") or 0, 0) for b in unmatched}, reverse=True)
    for b in unmatched:
        s = round(b.style.get("size") or 0, 0)
        if floor_size is not None and s >= floor_size - 0.5:
            b.level = top or 1
        else:
            b.level = (top or 0) + 1 + sizes.index(s)


def assign_list_levels(blocks: list[Block]) -> None:
    """List nesting depth from indentation within each run of list items."""
    stack: list[float] = []
    for b in blocks:
        if b.role != "list_item":
            if b.role in ("heading", "table", "code", "picture"):
                stack = []
            continue
        x = b.bbox[0]
        if not stack:
            stack = [x]
        elif x > stack[-1] + 6:
            stack.append(x)
        else:
            while len(stack) > 1 and x < stack[-1] - 6:
                stack.pop()
        b.level = len(stack) - 1


PAGE_KIND_BY_TITLE = [
    (re.compile(r"^cover$"), "cover"),
    (re.compile(r"^(brief )?contents$|^table of contents$"), "toc"),
    (re.compile(r"^index$"), "index"),
    (re.compile(r"^(bibliography|references)$"), "bibliography"),
    (re.compile(r"^copyright"), "copyright"),
]


def classify_pages(
    first: int, last: int, page_count: int, outline: list[list], blocks: list[Block]
) -> dict[int, str]:
    kinds = {p: "body" for p in range(first, last + 1)}
    top = sorted([(page, _norm_title(title)) for lvl, title, page, *_ in outline if lvl == 1])
    for i, (start, title) in enumerate(top):
        end = (top[i + 1][0] - 1) if i + 1 < len(top) else page_count
        for regex, kind in PAGE_KIND_BY_TITLE:
            if regex.search(title):
                if kind == "cover":  # the bookmark covers the cover only, not what follows
                    end = start
                for p in range(max(start, first), min(end, last) + 1):
                    kinds[p] = kind
    first_chapter = next(
        (page for page, t in top if t.startswith("chapter") or t[:1].isdigit()), None
    )
    body_text: dict[int, str] = defaultdict(str)
    for b in blocks:
        if b.role not in ("header", "footer"):
            body_text[b.page_no] += " " + b.text.lower()
    legal = ("isbn", "library of congress", "all rights reserved", "rights restrictions")
    for p in kinds:
        text = body_text.get(p, "")
        if not text.strip():
            kinds[p] = "blank" if kinds[p] == "body" else kinds[p]
        elif kinds[p] == "body" and first_chapter and p < first_chapter:
            if any(k in text for k in legal):
                kinds[p] = "copyright"
            elif len(text.split()) < 60:
                kinds[p] = "title"  # title page, dedication, publisher list
    return kinds
