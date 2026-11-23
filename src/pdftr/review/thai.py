"""R4: review the Thai chunks."""

from __future__ import annotations

import html
import re

import psycopg
from psycopg.types.json import Jsonb

from pdftr.ingest.textutil import strip_markup
from pdftr.render.contents import entries as contents_entries
from pdftr.render.contents import exclusions, is_contents
from pdftr.render.contents import key as content_key
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


# ------------------------------------------------- other Thai text on the page
OTHER_LABEL = {"heading": "Heading", "header": "Page header", "toc": "Contents", "table": "Table"}


def other_text(conn: psycopg.Connection, doc_id: int, page_no: int) -> list[dict]:
    """Thai on the page that is not a chunk: headings and the running header, contents and
    table cells, and bookmark titles. Each item says how to save an edit."""
    items: list[dict] = []
    rows = conn.execute(
        """SELECT b.id, b.role, b.text_en, b.markup_th, b.tr_source, b.style, b.cells_th,
                  b.x0, b.y0, b.x1, b.y1, p.kind AS page_kind
           FROM blocks b JOIN pages p ON p.doc_id = b.doc_id AND p.page_no = b.page_no
           WHERE b.doc_id=%s AND b.page_no=%s AND b.translate
             AND b.role IN ('heading', 'header', 'toc', 'table')
             AND (b.role IN ('toc', 'table') OR NOT EXISTS (
                  SELECT 1 FROM chunks c WHERE c.doc_id = b.doc_id AND b.id = ANY(c.block_ids)))
           ORDER BY b.ord""",
        (doc_id, page_no),
    ).fetchall()
    base = f"/doc/{doc_id}"
    for b in rows:
        label = "Contents" if b["page_kind"] == "toc" else OTHER_LABEL[b["role"]]
        if b["role"] in ("heading", "header"):
            if b["markup_th"] is not None:
                items.append({
                    "label": label, "en": b["text_en"], "th": strip_markup(b["markup_th"]),
                    "source": b["tr_source"], "url": f"{base}/blocks/{b['id']}/th/edit", "pos": None,
                })  # fmt: skip
            continue
        cells_th = b["cells_th"] or {}
        if is_contents(b):
            for e in _contents_entries(conn, doc_id, page_no, b):
                if cells_th.get(content_key(e.title)):
                    items.append({
                        "label": label, "en": e.title, "th": cells_th[content_key(e.title)],
                        "source": None, "url": f"{base}/blocks/{b['id']}/cells/th/edit",
                        "pos": content_key(e.title),
                    })  # fmt: skip
            continue
        for cell in (b["style"] or {}).get("cells") or []:
            pos = f"{cell['row']},{cell['col']}"
            if cells_th.get(pos):
                items.append({
                    "label": label, "en": cell.get("text") or "", "th": cells_th[pos],
                    "source": None, "url": f"{base}/blocks/{b['id']}/cells/th/edit", "pos": pos,
                })  # fmt: skip
    for e in conn.execute(
        "SELECT * FROM outline_entries WHERE doc_id=%s AND page_no=%s ORDER BY seq",
        (doc_id, page_no),
    ).fetchall():
        items.append({
            "label": "Bookmark", "en": e["title_en"], "th": e["title_th"] or "",
            "source": e["tr_source"], "url": f"{base}/outline/{e['id']}/th/edit", "pos": None,
        })  # fmt: skip
    return items


def _contents_entries(conn: psycopg.Connection, doc_id: int, page_no: int, block: dict):
    """The entries of a contents block, read from the PDF (see pdftr.render.contents)."""
    import pymupdf

    path = conn.execute("SELECT file_path FROM documents WHERE id=%s", (doc_id,)).fetchone()
    others = conn.execute(
        "SELECT role, x0, y0, x1, y1, translate FROM blocks WHERE doc_id=%s AND page_no=%s",
        (doc_id, page_no),
    ).fetchall()
    with pymupdf.open(path["file_path"]) as pdf:
        rect = (block["x0"], block["y0"], block["x1"], block["y1"])
        return contents_entries(pdf[page_no - 1], rect, exclusions(others))


def _clean(text: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        raise ReviewError("The Thai text cannot be empty.")
    return text


def edit_block_thai(conn: psycopg.Connection, doc_id: int, block_id: int, text: str) -> None:
    """Your Thai for a heading or running header; a heading's contents title and
    bookmark follow it unless you edited those yourself."""
    from pdftr.translate.titles import norm_title

    text = _clean(text)
    b = conn.execute(
        "SELECT * FROM blocks WHERE id=%s AND doc_id=%s", (block_id, doc_id)
    ).fetchone()
    if b is None or b["role"] not in ("heading", "header"):
        raise ReviewError("Only headings and page headers are edited here.")
    markup = html.escape(text, quote=False)
    conn.execute(
        "UPDATE blocks SET markup_th=%s, tr_source='human', tr_flags='{}' WHERE id=%s",
        (markup, block_id),
    )
    memory.human_put(conn, b["markup_en"] or html.escape(b["text_en"], quote=False), markup)
    if b["role"] == "heading":
        key = norm_title(b["text_en"])
        for e in conn.execute(
            "SELECT id, title_en FROM outline_entries WHERE doc_id=%s "
            "AND coalesce(tr_source, '') <> 'human'",
            (b["doc_id"],),
        ).fetchall():
            if norm_title(e["title_en"]) == key:
                conn.execute(
                    "UPDATE outline_entries SET title_th=%s, tr_source='heading', tr_flags='{}' "
                    "WHERE id=%s",
                    (text, e["id"]),
                )
        for t in conn.execute(
            "SELECT id, style, cells_th FROM blocks WHERE doc_id=%s AND role='toc'", (b["doc_id"],)
        ).fetchall():
            cells_th = dict(t["cells_th"] or {})
            changed = False
            for cell in (t["style"] or {}).get("cells") or []:
                pos = f"{cell['row']},{cell['col']}"
                if pos in cells_th and norm_title(cell.get("text") or "") == key:
                    cells_th[pos], changed = text, True
            if changed:
                conn.execute(
                    "UPDATE blocks SET cells_th=%s WHERE id=%s", (Jsonb(cells_th), t["id"])
                )
    log(conn, b["doc_id"], "r4", "edit_block", f"block:{block_id}", new=text)


def edit_cell_thai(
    conn: psycopg.Connection, doc_id: int, block_id: int, pos: str, text: str
) -> None:
    """Your Thai for one contents or table cell."""
    from pdftr.translate.runner import _table_markdown

    text = _clean(text)
    b = conn.execute(
        """SELECT b.*, p.kind AS page_kind FROM blocks b
           JOIN pages p ON p.doc_id = b.doc_id AND p.page_no = b.page_no
           WHERE b.id=%s AND b.doc_id=%s""",
        (block_id, doc_id),
    ).fetchone()
    if b is None or b["role"] not in ("toc", "table"):
        raise ReviewError("Only contents and table cells are edited here.")
    if is_contents(b):
        entry = next(
            (e for e in _contents_entries(conn, doc_id, b["page_no"], b)
             if content_key(e.title) == pos),
            None,
        )  # fmt: skip
        source = entry.title if entry else None
    else:
        cell = next(
            (c for c in (b["style"] or {}).get("cells") or [] if f"{c['row']},{c['col']}" == pos),
            None,
        )
        source = (cell.get("text") or "") if cell else None
    if source is None:
        raise ReviewError("That cell does not exist.")
    cells_th = dict(b["cells_th"] or {})
    cells_th[pos] = text
    conn.execute("UPDATE blocks SET cells_th=%s WHERE id=%s", (Jsonb(cells_th), block_id))
    memory.human_put(conn, html.escape(source, quote=False), html.escape(text))
    for c in conn.execute(
        "SELECT id, block_ids FROM chunks WHERE kind='table' AND %s = ANY(block_ids)", (block_id,)
    ).fetchall():
        conn.execute(
            "UPDATE chunks SET text_th=%s WHERE id=%s",
            (_table_markdown(conn, c["block_ids"]), c["id"]),
        )
    log(conn, b["doc_id"], "r4", "edit_cell", f"block:{block_id}:{pos}", new=text)


def edit_outline_thai(conn: psycopg.Connection, doc_id: int, entry_id: int, text: str) -> None:
    from pdftr.translate.titles import set_outline_thai

    text = _clean(text)
    e = conn.execute(
        "SELECT doc_id FROM outline_entries WHERE id=%s AND doc_id=%s", (entry_id, doc_id)
    ).fetchone()
    if e is None:
        raise ReviewError("That bookmark does not exist.")
    set_outline_thai(conn, entry_id, text)
    log(conn, e["doc_id"], "r4", "edit_bookmark", f"outline:{entry_id}", new=text)
