"""Persist ingest results to PostgreSQL."""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from pdftr.ingest.models import Block, Chunk, TermCandidate


def find_document(conn: psycopg.Connection, file_hash: str) -> dict | None:
    return conn.execute("SELECT * FROM documents WHERE file_hash = %s", (file_hash,)).fetchone()


def create_document(
    conn: psycopg.Connection,
    *,
    file_hash: str,
    file_name: str,
    file_path: str,
    title: str | None,
    page_count: int,
) -> int:
    row = conn.execute(
        """INSERT INTO documents (file_hash, file_name, file_path, title, page_count)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (file_hash, file_name, file_path, title, page_count),
    ).fetchone()
    return row["id"]


def delete_document(conn: psycopg.Connection, doc_id: int) -> None:
    conn.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
    # drop collected terms no document uses any more, unless someone reviewed them
    conn.execute(
        """DELETE FROM terms t
           WHERE t.source = 'auto' AND t.review_en = 'pending' AND t.review_th = 'pending'
             AND NOT EXISTS (SELECT 1 FROM doc_terms dt WHERE dt.term_id = t.id)"""
    )


def ingested_pages(conn: psycopg.Connection, doc_id: int) -> set[int]:
    rows = conn.execute("SELECT page_no FROM pages WHERE doc_id = %s", (doc_id,)).fetchall()
    return {r["page_no"] for r in rows}


def save_pages(conn: psycopg.Connection, doc_id: int, pages: dict[int, dict]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO pages (doc_id, page_no, label, kind, width, height, flags)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    doc_id,
                    p,
                    info["label"],
                    info["kind"],
                    info["width"],
                    info["height"],
                    info.get("flags", []),
                )
                for p, info in sorted(pages.items())
            ],
        )


def save_blocks(conn: psycopg.Connection, doc_id: int, blocks: list[Block]) -> None:
    with conn.cursor() as cur:
        for b in blocks:
            cur.execute(
                """INSERT INTO blocks (doc_id, page_no, ord, role, docling_label, level,
                                      x0, y0, x1, y1, markup_en, text_en, style, translate, flags)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    doc_id,
                    b.page_no,
                    b.ord,
                    b.role,
                    b.docling_label,
                    b.level,
                    *b.bbox,
                    b.markup,
                    b.text,
                    Jsonb(b.style | ({"marker": b.marker} if b.marker else {})),
                    b.translate,
                    b.flags,
                ),
            )
            b.db_id = cur.fetchone()["id"]


def save_chunks(conn: psycopg.Connection, doc_id: int, chunks: list[Chunk]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO chunks (doc_id, ord, part, kind, section_path, context_en, text_en,
                                  markup_en, block_ids, page_start, page_end, list_level,
                                  embed, flags)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [
                (
                    doc_id,
                    c.ord,
                    c.part,
                    c.kind,
                    c.section_path,
                    c.context,
                    c.text,
                    c.markup,
                    [b.db_id for b in c.blocks],
                    c.page_start,
                    c.page_end,
                    c.list_level,
                    c.embed,
                    sorted(set(c.flags)),
                )
                for c in chunks
            ],
        )


def save_terms(conn: psycopg.Connection, doc_id: int, terms: list[TermCandidate]) -> None:
    from pdftr.glossary import default_for

    with conn.cursor() as cur:
        for t in terms:
            d = default_for(t.norm, t.kind, t.sources)
            cur.execute(
                """INSERT INTO terms (norm, term_en, kind, decision, review_en, term_th, th_source)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (norm) DO UPDATE SET
                     -- refresh only what nobody has reviewed or entered by hand
                     term_en = CASE WHEN terms.source = 'auto' AND terms.review_en = 'pending'
                                    THEN EXCLUDED.term_en ELSE terms.term_en END,
                     kind    = CASE WHEN terms.source = 'auto' AND terms.review_en = 'pending'
                                    THEN EXCLUDED.kind ELSE terms.kind END,
                     updated_at = now()
                   RETURNING id""",
                (
                    t.norm,
                    t.term,
                    t.kind,
                    d["decision"],
                    d["review_en"],
                    d["term_th"],
                    d["th_source"],
                ),
            )
            term_id = cur.fetchone()["id"]
            cur.execute(
                """INSERT INTO doc_terms (doc_id, term_id, freq, first_page, sources, example)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (doc_id, term_id) DO UPDATE SET
                     freq = doc_terms.freq + EXCLUDED.freq,
                     first_page = LEAST(doc_terms.first_page, EXCLUDED.first_page),
                     sources = (SELECT array_agg(DISTINCT s)
                                FROM unnest(doc_terms.sources || EXCLUDED.sources) s),
                     example = COALESCE(doc_terms.example, EXCLUDED.example)""",
                (doc_id, term_id, t.freq, t.first_page, sorted(t.sources), t.example),
            )


def start_job(conn: psycopg.Connection, doc_id: int | None, kind: str, params: dict) -> int:
    row = conn.execute(
        "INSERT INTO jobs (doc_id, kind, status, params) VALUES (%s, %s, 'running', %s) RETURNING id",
        (doc_id, kind, Jsonb(params)),
    ).fetchone()
    conn.commit()
    return row["id"]


def finish_job(
    conn: psycopg.Connection, job_id: int, status: str, progress: dict, error: str | None = None
) -> None:
    conn.execute(
        "UPDATE jobs SET status=%s, progress=%s, error=%s, finished_at=now() WHERE id=%s",
        (status, Jsonb(progress), error, job_id),
    )
    conn.commit()
