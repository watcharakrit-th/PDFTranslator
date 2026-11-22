"""Thai page previews for R4, cached by the page's Thai content."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import psycopg

from pdftr.render.thai_page import RENDERER_VERSION, Fit, render_thai_page

PREVIEW_ZOOM = 1.6


def page_blocks(conn: psycopg.Connection, doc_id: int, page_no: int) -> list[dict]:
    return conn.execute(
        """SELECT id, role, x0, y0, x1, y1, style, markup_th, cells_th, translate
           FROM blocks WHERE doc_id=%s AND page_no=%s ORDER BY ord""",
        (doc_id, page_no),
    ).fetchall()


def preview(
    conn: psycopg.Connection, doc_id: int, page_no: int, data_dir: Path
) -> tuple[Path, list[Fit]]:
    """PNG of the Thai page (rendered once per content version) and the fit report."""
    doc = conn.execute("SELECT file_path FROM documents WHERE id=%s", (doc_id,)).fetchone()
    blocks = page_blocks(conn, doc_id, page_no)
    content = [RENDERER_VERSION] + [[b["id"], b["markup_th"], b["cells_th"]] for b in blocks]
    digest = hashlib.sha256(
        json.dumps(content, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:12]
    folder = data_dir / f"doc_{doc_id}" / "thai"
    png = folder / f"p{page_no:04d}-{digest}.png"
    report = png.with_suffix(".json")
    if png.exists() and report.exists():
        fits = [Fit(**f) for f in json.loads(report.read_text(encoding="utf-8"))]
        return png, fits
    import pymupdf

    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob(f"p{page_no:04d}-*"):
        old.unlink(missing_ok=True)
    rendered, fits = render_thai_page(doc["file_path"], page_no, blocks)
    rendered[0].get_pixmap(matrix=pymupdf.Matrix(PREVIEW_ZOOM, PREVIEW_ZOOM)).save(png)
    report.write_text(json.dumps([asdict(f) for f in fits]), encoding="utf-8")
    return png, fits
