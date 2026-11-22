"""Ingest = layout analysis + exact text + roles + cutter + term candidates -> database."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from pdftr.settings import configure_environment, get_settings


class AlreadyIngested(Exception):
    pass


def document_title(meta_title: str | None, stem: str) -> str:
    """The PDF's own title, unless it is missing or all capitals (often an internal
    code such as "3P-EBK: ..."); then the file name without trailing "(...)" notes."""
    title = (meta_title or "").strip()
    if title and not title.isupper():
        return title
    return re.sub(r"(\s*\([^()]*\))+\s*$", "", stem).strip() or stem


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest(
    pdf_path: Path,
    first: int,
    last: int,
    *,
    force: bool = False,
    use_db: bool = True,
    log: Callable[[str], None] = print,
) -> dict:
    settings = get_settings()
    configure_environment(settings)

    import pymupdf

    from pdftr.db import connect, init_schema
    from pdftr.ingest import store

    pdf_path = pdf_path.resolve()
    pdf = pymupdf.open(pdf_path)
    last = min(last, pdf.page_count)
    if first < 1 or first > last:
        raise ValueError(f"bad page range {first}-{last} (document has {pdf.page_count} pages)")
    file_hash = sha256_file(pdf_path)

    if not use_db:
        return _run(pdf, pdf_path, file_hash, first, last, None, None, log)

    conn = connect()
    try:
        init_schema(conn)
        existing = store.find_document(conn, file_hash)
        if existing and force:
            log(f"--force: deleting document {existing['id']} and all its reviews")
            store.delete_document(conn, existing["id"])
            conn.commit()
            existing = None
        doc_id = None
        if existing:
            done = store.ingested_pages(conn, existing["id"]) & set(range(first, last + 1))
            if done:
                raise AlreadyIngested(
                    f"document {existing['id']} already has pages {min(done)}-{max(done)}; "
                    "use --force to start over"
                )
            doc_id = existing["id"]
        job_id = store.start_job(conn, doc_id, "ingest", {"first": first, "last": last})
        try:
            summary = _run(pdf, pdf_path, file_hash, first, last, conn, doc_id, log, job_id)
        except BaseException as exc:
            conn.rollback()
            store.finish_job(conn, job_id, "failed", {}, error=repr(exc)[:2000])
            raise
        store.finish_job(conn, job_id, "done", summary)
        return summary
    finally:
        conn.close()


def _run(
    pdf,
    pdf_path: Path,
    file_hash: str,
    first: int,
    last: int,
    conn,
    doc_id,
    log: Callable[[str], None],
    job_id: int | None = None,
) -> dict:
    from pdftr.gpu import GpuGuard
    from pdftr.ingest import store
    from pdftr.ingest.blocks import (
        assign_heading_levels,
        assign_list_levels,
        build_page_blocks,
        classify_pages,
        mark_boilerplate,
    )
    from pdftr.ingest.cutter import SKIP_PAGE_KINDS, cut
    from pdftr.ingest.docling_extract import extract_items
    from pdftr.ingest.pdf_spans import fill_boxes, page_lines
    from pdftr.ingest.terms import collect_terms, key_term_lines
    from pdftr.ingest.textutil import build_vocabulary

    settings = get_settings()
    t_start = time.monotonic()

    # 1) layout analysis (GPU, small batches, temperature checkpoints)
    guard = GpuGuard.from_settings(settings, log=log)
    log(
        f"Layout analysis: pages {first}-{last} in batches of {settings.docling_batch_pages} "
        f"(GPU hot spot: pause at {settings.gpu_pause_at_c}°C)"
    )
    cache_dir = settings.data_dir / "layout_cache" / file_hash[:16]
    items = extract_items(pdf_path, first, last, settings, guard, log, cache_dir=cache_dir)
    by_page: dict[int, list] = defaultdict(list)
    for it in items:
        by_page[it.page_no].append(it)
    t_layout = time.monotonic()

    # 2) exact text + styles per page (CPU)
    pages_info: dict[int, dict] = {}
    lines_by_page, fills_by_page = {}, {}
    for p in range(first, last + 1):
        page = pdf[p - 1]
        lines_by_page[p] = page_lines(page)
        fills_by_page[p] = fill_boxes(page)
        pages_info[p] = {
            "label": page.get_label() or str(p),
            "width": page.rect.width,
            "height": page.rect.height,
        }
    vocab = build_vocabulary([ln.text for lines in lines_by_page.values() for ln in lines])

    blocks = []
    for p in range(first, last + 1):
        blocks += build_page_blocks(
            p,
            (pages_info[p]["width"], pages_info[p]["height"]),
            by_page.get(p, []),
            lines_by_page[p],
            fills_by_page[p],
            vocab,
        )
    mark_boilerplate(blocks, {p: i["height"] for p, i in pages_info.items()}, last - first + 1)
    outline = pdf.get_toc()
    assign_heading_levels(blocks, outline)
    assign_list_levels(blocks)
    kinds = classify_pages(first, last, pdf.page_count, outline, blocks)
    for p, k in kinds.items():
        pages_info[p]["kind"] = k
    for b in blocks:
        # what the Thai page will redraw: not pictures, formulas, code, the copyright
        # footer, bare page numbers, or pages we leave in English
        b.translate = (
            b.role not in ("picture", "formula", "code", "footer")
            and kinds[b.page_no]
            not in ("cover", "title", "copyright", "index", "bibliography", "blank")
            and not (b.role == "header" and b.text.strip().isdigit())
        )

    # 3) cutter + terms
    chunks = cut(blocks, kinds, vocab)
    skip_pages = {p for p, k in kinds.items() if k in SKIP_PAGE_KINDS}
    terms = collect_terms(blocks, chunks, skip_pages, key_term_lines(chunks, lines_by_page))
    t_cut = time.monotonic()

    # 4) persist (one transaction) + JSON dump for inspection
    if conn is not None:
        with conn.transaction():
            if doc_id is None:
                doc_id = store.create_document(
                    conn,
                    file_hash=file_hash,
                    file_name=pdf_path.name,
                    file_path=str(pdf_path),
                    title=document_title((pdf.metadata or {}).get("title"), pdf_path.stem),
                    page_count=pdf.page_count,
                )
                conn.execute("UPDATE jobs SET doc_id=%s WHERE id=%s", (doc_id, job_id))
            store.save_pages(conn, doc_id, pages_info)
            store.save_blocks(conn, doc_id, blocks)
            store.save_chunks(conn, doc_id, chunks)
            store.save_terms(conn, doc_id, terms)
            conn.execute("UPDATE documents SET updated_at=now() WHERE id=%s", (doc_id,))

    out_dir = get_settings().data_dir / f"doc_{doc_id or file_hash[:8]}"
    out_dir.mkdir(parents=True, exist_ok=True)
    dump = out_dir / f"ingest_p{first}-{last}.json"
    dump.write_text(
        json.dumps(
            {
                "pages": pages_info,
                "blocks": [asdict(b) for b in blocks],
                "chunks": [
                    {
                        **{k: v for k, v in asdict(c).items() if k != "blocks"},
                        "block_ords": [b.ord for b in c.blocks],
                    }
                    for c in chunks
                ],
                "terms": [{**asdict(t), "sources": sorted(t.sources)} for t in terms],
            },
            ensure_ascii=False,
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )

    return {
        "doc_id": doc_id,
        "pages": f"{first}-{last}",
        "page_kinds": dict(Counter(kinds.values())),
        "blocks": len(blocks),
        "block_roles": dict(Counter(b.role for b in blocks)),
        "chunks": len(chunks),
        "chunks_embedded": sum(c.embed for c in chunks),
        "chunk_kinds": dict(Counter(c.kind for c in chunks)),
        "chunk_flags": dict(Counter(f for c in chunks for f in set(c.flags))),
        "terms": len(terms),
        "term_kinds": dict(Counter(t.kind for t in terms)),
        "seconds_layout": round(t_layout - t_start, 1),
        "seconds_cut_terms": round(t_cut - t_layout, 1),
        "seconds_total": round(time.monotonic() - t_start, 1),
        "gpu_peak_hotspot_c": guard.peak_c,
        "gpu_paused_s": round(guard.paused_s),
        "dump": str(dump),
    }
