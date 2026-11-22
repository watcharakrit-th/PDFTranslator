"""R4: review the Thai chunks."""

from __future__ import annotations

import html
import re

import psycopg

from pdftr.ingest.textutil import strip_markup
from pdftr.review import ReviewError
from pdftr.review.chunks import get_chunk, log
from pdftr.review.markup import retag
from pdftr.translate import memory
from pdftr.translate.engine import Engine
from pdftr.translate.rules import load_glossary
from pdftr.translate.runner import Glossary, apply_to_blocks, translate_text

TR_SOURCE_LABEL = {
    "mt": "machine",
    "tm": "memory",
    "human": "you",
    "copy": "code (copied)",
    "kept": "kept in English",
}


def add_glossary_issues(conn: psycopg.Connection, doc_id: int, chunks: list[dict]) -> None:
    """Spell out the "glossary" flag on each card: which terms differ from R3."""
    from pdftr.translate.checks import glossary_issues
    from pdftr.translate.protect import protect
    from pdftr.translate.rules import present_terms

    flagged = [c for c in chunks if "glossary" in (c.get("tr_flags") or []) and c.get("text_th")]
    if not flagged:
        return
    terms = load_glossary(conn, doc_id)
    for c in flagged:
        source = protect(c["markup_en"] or html.escape(c["text_en"], quote=False)).text
        c["glossary_issues"] = glossary_issues(c["text_th"], present_terms(source, terms))


def progress(conn: psycopg.Connection, doc_id: int) -> dict:
    row = conn.execute(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE review_th = 'approved') AS approved,
                  count(*) FILTER (WHERE review_th = 'pending') AS pending,
                  count(*) FILTER (WHERE review_th = 'pending' AND tr_flags <> '{}') AS flagged,
                  count(*) FILTER (WHERE text_th IS NULL) AS missing
           FROM chunks WHERE doc_id = %s""",
        (doc_id,),
    ).fetchone()
    row["percent"] = round(100 * row["approved"] / row["total"]) if row["total"] else 100
    return row


def flagged_pages(conn: psycopg.Connection, doc_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT page_start AS p FROM chunks WHERE doc_id=%s AND review_th='pending' "
        "AND tr_flags <> '{}' ORDER BY 1",
        (doc_id,),
    ).fetchall()
    return [r["p"] for r in rows]


def pending_pages(conn: psycopg.Connection, doc_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT page_start AS p FROM chunks WHERE doc_id=%s AND review_th='pending' "
        "ORDER BY 1",
        (doc_id,),
    ).fetchall()
    return [r["p"] for r in rows]


def set_approved(conn: psycopg.Connection, chunk_id: int, approved: bool) -> None:
    c = get_chunk(conn, chunk_id)
    if approved and c["text_th"] is None:
        raise ReviewError("This chunk has no Thai yet.")
    conn.execute(
        "UPDATE chunks SET review_th=%s WHERE id=%s",
        ("approved" if approved else "pending", chunk_id),
    )
    log(conn, c["doc_id"], "r4", "approve" if approved else "unapprove", f"chunk:{chunk_id}")


def edit_thai(conn: psycopg.Connection, chunk_id: int, text: str) -> None:
    """Your Thai replaces the machine's, is approved, and is remembered for the same
    English text in any book (translation memory)."""
    c = get_chunk(conn, chunk_id)
    text = "\n".join(ln.strip() for ln in text.replace("\r\n", "\n").split("\n")).strip()
    if not text:
        raise ReviewError("The Thai text cannot be empty.")
    if c["kind"] in ("code", "table"):
        raise ReviewError("Code and tables are not edited here.")
    markup_th = retag(c["markup_th"] or "", text) if c["markup_th"] else html.escape(text)
    conn.execute(
        """UPDATE chunks SET markup_th=%s, text_th=%s, tr_source='human', tr_flags='{}',
                  review_th='approved', translated_at=now() WHERE id=%s""",
        (markup_th, strip_markup(markup_th), chunk_id),
    )
    if "mcq" in c["flags"]:
        _mcq_to_blocks(conn, c, markup_th)
    else:
        memory.human_put(conn, c["markup_en"] or html.escape(c["text_en"], quote=False), markup_th)
        apply_to_blocks(conn, c["doc_id"], c["page_start"], c["page_end"])
    log(conn, c["doc_id"], "r4", "edit", f"chunk:{chunk_id}", old=c["text_th"], new=text)


def _mcq_to_blocks(conn: psycopg.Connection, c: dict, markup_th: str) -> None:
    """Question + options: line n of the Thai belongs to block n (markers removed)."""
    lines = [ln.strip() for ln in markup_th.split("\n") if ln.strip()]
    if len(lines) != len(c["block_ids"]):
        raise ReviewError(
            f"Keep one line per part: the question and {len(c['block_ids']) - 1} options."
        )
    rows = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, markup_en, text_en FROM blocks WHERE id = ANY(%s)", (c["block_ids"],)
        ).fetchall()
    }
    for i, (block_id, line) in enumerate(zip(c["block_ids"], lines, strict=True)):
        if i > 0:
            line = re.sub(r"^\(?[A-Za-z][.)]\s+", "", line)
        conn.execute(
            "UPDATE blocks SET markup_th=%s, tr_source='human', tr_flags='{}' WHERE id=%s",
            (line, block_id),
        )
        source = rows[block_id]["markup_en"] or html.escape(rows[block_id]["text_en"], quote=False)
        memory.human_put(conn, source, line)


def retranslate(conn: psycopg.Connection, chunk_id: int, engine: Engine) -> None:
    """Ask the engine again (at a higher temperature) for one chunk."""
    c = get_chunk(conn, chunk_id)
    if c["kind"] in ("code", "table") or "mcq" in c["flags"]:
        raise ReviewError(
            "Only paragraphs, list items, notes and captions can be re-translated one by one here."
        )
    glossary = Glossary(load_glossary(conn, c["doc_id"]))
    source = c["markup_en"] or html.escape(c["text_en"], quote=False)
    markup_th, key, outcome = translate_text(conn, engine, glossary, source, retry=True)
    conn.execute(
        """UPDATE chunks SET markup_th=%s, text_th=%s, tr_source='mt', tr_flags=%s, tr_key=%s,
                  review_th='pending', translated_at=now() WHERE id=%s""",
        (markup_th, strip_markup(markup_th), list(outcome.flags), key, chunk_id),
    )
    apply_to_blocks(conn, c["doc_id"], c["page_start"], c["page_end"])
    log(conn, c["doc_id"], "r4", "retranslate", f"chunk:{chunk_id}")


def approve_page(conn: psycopg.Connection, doc_id: int, page_no: int) -> int:
    n = conn.execute(
        "UPDATE chunks SET review_th='approved' WHERE doc_id=%s AND %s BETWEEN page_start "
        "AND page_end AND review_th='pending' AND text_th IS NOT NULL",
        (doc_id, page_no),
    ).rowcount
    log(conn, doc_id, "r4", "approve_page", f"page:{page_no}", count=n)
    return n


def approve_chapter(conn: psycopg.Connection, doc_id: int, page_no: int) -> tuple[str, int]:
    row = conn.execute(
        "SELECT coalesce(section_path[1], '') AS title FROM chunks WHERE doc_id=%s "
        "AND %s BETWEEN page_start AND page_end ORDER BY ord, part LIMIT 1",
        (doc_id, page_no),
    ).fetchone()
    if row is None:
        raise ReviewError("This page has no chunks.")
    n = conn.execute(
        "UPDATE chunks SET review_th='approved' WHERE doc_id=%s AND "
        "coalesce(section_path[1], '') = %s AND review_th='pending' AND text_th IS NOT NULL",
        (doc_id, row["title"]),
    ).rowcount
    log(conn, doc_id, "r4", "approve_chapter", f"page:{page_no}", chapter=row["title"], count=n)
    return row["title"] or "(front matter)", n


def approve_all(conn: psycopg.Connection, doc_id: int) -> int:
    n = conn.execute(
        "UPDATE chunks SET review_th='approved' WHERE doc_id=%s AND review_th='pending' "
        "AND text_th IS NOT NULL",
        (doc_id,),
    ).rowcount
    log(conn, doc_id, "r4", "approve_all", f"doc:{doc_id}", count=n)
    return n
