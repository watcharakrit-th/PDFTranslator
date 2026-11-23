"""Rendered Thai pages, cached by the page's Thai content.

R4 shows the PNG; the book renderer uses the PDF. A page is rendered again only
when its Thai (or the renderer) changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import psycopg

from pdftr.render.thai_page import RENDERER_VERSION, Fit, render_thai_page

PREVIEW_ZOOM = 1.6


@dataclass
class ThaiPage:
    pdf: Path
    png: Path
    fits: list[Fit]


def page_blocks(conn: psycopg.Connection, doc_id: int, page_no: int) -> list[dict]:
    return conn.execute(
        """SELECT b.id, b.role, b.x0, b.y0, b.x1, b.y1, b.style, b.markup_th, b.cells_th,
                  b.translate, p.kind AS page_kind
           FROM blocks b JOIN pages p ON p.doc_id = b.doc_id AND p.page_no = b.page_no
           WHERE b.doc_id=%s AND b.page_no=%s ORDER BY b.ord""",
        (doc_id, page_no),
    ).fetchall()


def has_thai(blocks: list[dict]) -> bool:
    return any(b["markup_th"] or b["cells_th"] for b in blocks)


def thai_page(
    conn: psycopg.Connection,
    doc_id: int,
    page_no: int,
    data_dir: Path,
    *,
    src=None,
    png: bool = True,
) -> ThaiPage:
    """The Thai page as PDF (and PNG), rendered once per content version.

    ``src`` is the book as an open document, to avoid reopening it page by page.
    """
    import pymupdf

    blocks = page_blocks(conn, doc_id, page_no)
    content = [RENDERER_VERSION] + [[b["id"], b["markup_th"], b["cells_th"]] for b in blocks]
    digest = hashlib.sha256(
        json.dumps(content, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:12]
    folder = data_dir / f"doc_{doc_id}" / "thai"
    stem = folder / f"p{page_no:04d}-{digest}"
    pdf_path, png_path, report = (stem.with_suffix(s) for s in (".pdf", ".png", ".json"))
    if pdf_path.exists() and report.exists() and (png_path.exists() or not png):
        fits = [Fit(**f) for f in json.loads(report.read_text(encoding="utf-8"))]
        return ThaiPage(pdf_path, png_path, fits)
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob(f"p{page_no:04d}-*"):
        if not old.name.startswith(stem.name):
            old.unlink(missing_ok=True)
    if src is None:
        row = conn.execute("SELECT file_path FROM documents WHERE id=%s", (doc_id,)).fetchone()
        src = row["file_path"]
    rendered, fits = render_thai_page(src, page_no, blocks)
    rendered.save(pdf_path, garbage=4, deflate=True)
    if png:
        rendered[0].get_pixmap(matrix=pymupdf.Matrix(PREVIEW_ZOOM, PREVIEW_ZOOM)).save(png_path)
    report.write_text(json.dumps([asdict(f) for f in fits]), encoding="utf-8")
    return ThaiPage(pdf_path, png_path, fits)


def preview(
    conn: psycopg.Connection, doc_id: int, page_no: int, data_dir: Path
) -> tuple[Path, list[Fit]]:
    """PNG of the Thai page for R4 and the fit report."""
    page = thai_page(conn, doc_id, page_no, data_dir)
    return page.png, page.fits
