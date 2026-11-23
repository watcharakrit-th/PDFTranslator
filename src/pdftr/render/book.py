"""The bilingual PDF: every English page next to its Thai page.

- ``side-by-side`` (default): one double-width sheet per page, English left, Thai right.
- ``alternate``: English page, then its Thai page.

Pages that are not translated (cover, title, copyright, index, bibliography, blank,
or simply no Thai yet) show the English page on both sides. Bookmarks follow the
book's outline with Thai titles, and page labels keep the book's own page numbers.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psycopg

from pdftr.render.preview import has_thai, page_blocks, thai_page
from pdftr.translate.titles import load_outline

LAYOUTS = ("side-by-side", "alternate")
COPY_KINDS = {"cover", "title", "copyright", "index", "bibliography", "blank"}


@dataclass
class BookResult:
    path: Path
    pages: int  # book pages rendered
    thai_pages: int  # of which with Thai drawn
    sheets: int  # pages in the output PDF
    overflow: int  # text areas that did not fit even at 60%
    seconds: float
    size_mb: float


def default_path(
    data_dir: Path, title: str, layout: str, first: int, last: int, full: bool
) -> Path:
    safe = re.sub(r'[<>:"/\\|?*]+', " ", title).strip() or "book"
    parts = ["EN-TH"] + ([layout] if layout != "side-by-side" else [])
    if not full:
        parts.append(f"p{first}-{last}")
    return data_dir / "output" / f"{safe} ({', '.join(parts)}).pdf"


def _label_rules(labels: list[str]) -> list[dict]:
    """Page-label rules: runs of 1, 2, 3 ... become one numbered rule, the rest prefixes."""
    rules: list[dict] = []
    for i, label in enumerate(labels):
        last = rules[-1] if rules else None
        if (
            label.isdigit()
            and last is not None
            and last["style"] == "D"
            and int(label) == last["firstpagenum"] + (i - last["startpage"])
        ):
            continue
        if label.isdigit():
            rules.append({"startpage": i, "prefix": "", "style": "D", "firstpagenum": int(label)})
        else:
            rules.append({"startpage": i, "prefix": label, "style": "", "firstpagenum": 1})
    return rules


def _bookmarks(entries: list[dict], first_sheet: dict[int, int]) -> list[list]:
    toc, previous = [], 0
    for e in entries:
        if e["page_no"] not in first_sheet:
            continue
        level = max(1, min(e["level"], previous + 1))
        toc.append([level, (e["title_th"] or e["title_en"]).strip(), first_sheet[e["page_no"]] + 1])
        previous = level
    return toc


def render_book(
    conn: psycopg.Connection,
    doc_id: int,
    data_dir: Path,
    *,
    layout: str = "side-by-side",
    first: int = 1,
    last: int = 10**9,
    out_path: Path | None = None,
    log: Callable[[str], None] = print,
) -> BookResult:
    import pymupdf

    if layout not in LAYOUTS:
        raise ValueError(f"layout must be one of {', '.join(LAYOUTS)}")
    started = time.monotonic()
    doc = conn.execute("SELECT * FROM documents WHERE id=%s", (doc_id,)).fetchone()
    if doc is None:
        raise ValueError(f"No document {doc_id}")
    pages = conn.execute(
        "SELECT page_no, kind FROM pages WHERE doc_id=%s AND page_no BETWEEN %s AND %s "
        "ORDER BY page_no",
        (doc_id, first, last),
    ).fetchall()
    if not pages:
        raise ValueError("No pages in that range")
    all_pages = conn.execute(
        "SELECT min(page_no) AS a, max(page_no) AS b FROM pages WHERE doc_id=%s", (doc_id,)
    ).fetchone()
    first_no, last_no = pages[0]["page_no"], pages[-1]["page_no"]
    full = (first_no, last_no) == (all_pages["a"], all_pages["b"])
    path = out_path or default_path(data_dir, doc["title"], layout, first_no, last_no, full)
    path.parent.mkdir(parents=True, exist_ok=True)

    src = pymupdf.open(doc["file_path"])
    out = pymupdf.open()
    first_sheet: dict[int, int] = {}
    labels: list[str] = []
    thai_count = overflow = 0
    for n, p in enumerate(pages, 1):
        index = p["page_no"] - 1
        english = src[index]
        thai_doc = None
        if p["kind"] not in COPY_KINDS and has_thai(page_blocks(conn, doc_id, p["page_no"])):
            rendered = thai_page(conn, doc_id, p["page_no"], data_dir, src=src, png=False)
            thai_doc = pymupdf.open(rendered.pdf)
            thai_count += 1
            overflow += sum(f.overflow for f in rendered.fits)
        label = english.get_label() or str(p["page_no"])
        first_sheet[p["page_no"]] = out.page_count
        if layout == "side-by-side":
            w, h = english.rect.width, english.rect.height
            sheet = out.new_page(width=2 * w, height=h)
            sheet.show_pdf_page(pymupdf.Rect(0, 0, w, h), src, index)
            if thai_doc is not None:
                sheet.show_pdf_page(pymupdf.Rect(w, 0, 2 * w, h), thai_doc, 0)
            else:
                sheet.show_pdf_page(pymupdf.Rect(w, 0, 2 * w, h), src, index)
            labels.append(label)
        else:
            out.insert_pdf(src, from_page=index, to_page=index, links=False)
            if thai_doc is not None:
                out.insert_pdf(thai_doc, links=False)
            else:
                out.insert_pdf(src, from_page=index, to_page=index, links=False)
            labels += [label, f"{label} TH"]
        if thai_doc is not None:
            thai_doc.close()
        if n % 25 == 0 or n == len(pages):
            log(f"  {n}/{len(pages)} pages · {thai_count} with Thai")

    toc = _bookmarks(load_outline(conn, doc_id), first_sheet)
    if toc:
        out.set_toc(toc)
    out.set_page_labels(_label_rules(labels))
    meta = src.metadata or {}
    out.set_metadata(
        {
            "title": f"{doc['title']} (English–Thai)",
            "author": meta.get("author") or "",
            "subject": "Bilingual English–Thai edition for personal study",
            "creator": "pdftr",
            "producer": "pdftr (PyMuPDF)",
        }
    )
    out.save(path, garbage=4, deflate=True)
    sheets = out.page_count
    out.close()
    src.close()
    return BookResult(
        path=path,
        pages=len(pages),
        thai_pages=thai_count,
        sheets=sheets,
        overflow=overflow,
        seconds=round(time.monotonic() - started, 1),
        size_mb=round(path.stat().st_size / 2**20, 1),
    )
