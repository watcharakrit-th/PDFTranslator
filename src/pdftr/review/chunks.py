"""R1: review the English chunks of a document."""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from pdftr.ingest.textutil import join_blocks, split_sentences, strip_markup
from pdftr.review import ReviewError
from pdftr.review.markup import retag

KINDS = (
    "paragraph", "list_item", "question", "answer", "callout",
    "caption", "footnote", "code", "table",
)  # fmt: skip

# Flags that ask for a human look (chunk flags and flags of the chunk's blocks).
REVIEW_FLAGS = ["joined_pages", "joined", "short", "split", "unknown_role"]
BLOCK_REVIEW_FLAGS = ["orphan", "text_mismatch", "multi_region", "demoted_heading"]

FLAG_HELP = {
    "joined_pages": "Joined across a page break: check that both parts belong together",
    "joined": "Joined from two blocks on the same page",
    "short": "Very short paragraph",
    "split": "Long paragraph, split at a sentence end for embedding",
    "unknown_role": "The layout model could not classify this text",
    "orphan": "Text the layout model missed; placed by its position on the page",
    "text_mismatch": "Extracted text differs from the layout model's text",
    "multi_region": "Spans several layout regions",
    "demoted_heading": "Looked like a heading but is a 'Label: value' line",
    "mcq": "Multiple-choice question with its options",
    "key_terms_list": "List of bare terms: kept for the glossary, not embedded",
    "merged": "Merged by you",
    "split_by_user": "Split by you",
    "edited": "Edited by you",
}

FLAGGED_SQL = """(c.flags && %(rf)s::text[] OR EXISTS (
    SELECT 1 FROM blocks b WHERE b.id = ANY(c.block_ids) AND b.flags && %(bf)s::text[]))"""


def _flag_params() -> dict:
    return {"rf": REVIEW_FLAGS, "bf": BLOCK_REVIEW_FLAGS}


def log(
    conn: psycopg.Connection, doc_id: int, step: str, action: str, target: str, **detail
) -> None:
    conn.execute(
        "INSERT INTO review_log (doc_id, step, action, target, detail) VALUES (%s,%s,%s,%s,%s)",
        (doc_id, step, action, target, Jsonb(detail)),
    )


def get_chunk(conn: psycopg.Connection, chunk_id: int) -> dict:
    row = conn.execute("SELECT * FROM chunks WHERE id = %s", (chunk_id,)).fetchone()
    if row is None:
        raise ReviewError(f"Chunk {chunk_id} no longer exists (merged or split?). Reload the page.")
    return row


# ----------------------------------------------------------------------- reading
def page_view(conn: psycopg.Connection, doc_id: int, page_no: int) -> dict:
    page = conn.execute(
        "SELECT page_no, label, kind, width, height FROM pages WHERE doc_id=%s AND page_no=%s",
        (doc_id, page_no),
    ).fetchone()
    if page is None:
        raise ReviewError(f"Page {page_no} has not been ingested.")
    blocks = conn.execute(
        "SELECT id, role, x0, y0, x1, y1, flags FROM blocks WHERE doc_id=%s AND page_no=%s "
        "ORDER BY ord",
        (doc_id, page_no),
    ).fetchall()
    chunks = conn.execute(
        f"""SELECT c.*, {FLAGGED_SQL} AS flagged,
                   coalesce((SELECT array_agg(DISTINCT f) FROM blocks b, unnest(b.flags) f
                             WHERE b.id = ANY(c.block_ids)), '{{}}') AS block_flags
            FROM chunks c
            WHERE c.doc_id = %(doc)s AND %(page)s BETWEEN c.page_start AND c.page_end
            ORDER BY c.ord, c.part""",
        {"doc": doc_id, "page": page_no, **_flag_params()},
    ).fetchall()
    for n, c in enumerate(chunks, 1):
        c["n"] = n
        c["split_points"] = split_points(conn, c)
    return {"page": page, "blocks": blocks, "chunks": chunks}


def split_points(conn: psycopg.Connection, c: dict) -> list[str]:
    """The pieces a chunk can be split between: its blocks, or its sentences."""
    if c["kind"] in ("code", "table"):
        return []
    if len(c["block_ids"]) > 1:
        rows = conn.execute(
            "SELECT id, markup_en, text_en FROM blocks WHERE id = ANY(%s)", (c["block_ids"],)
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        return [by_id[b]["markup_en"] or by_id[b]["text_en"] for b in c["block_ids"] if b in by_id]
    pieces = split_sentences(c["markup_en"] or c["text_en"])
    return pieces if len(pieces) > 1 else []


def progress(conn: psycopg.Connection, doc_id: int) -> dict:
    row = conn.execute(
        f"""SELECT count(*) AS total,
                   count(*) FILTER (WHERE review_en = 'approved') AS approved,
                   count(*) FILTER (WHERE review_en = 'pending') AS pending,
                   count(*) FILTER (WHERE review_en = 'pending' AND {FLAGGED_SQL}) AS flagged,
                   count(*) FILTER (WHERE NOT embed) AS skipped
            FROM chunks c WHERE c.doc_id = %(doc)s""",
        {"doc": doc_id, **_flag_params()},
    ).fetchone()
    row["percent"] = round(100 * row["approved"] / row["total"]) if row["total"] else 100
    return row


def flagged_pages(conn: psycopg.Connection, doc_id: int) -> list[int]:
    """Pages that still have a pending, flagged chunk."""
    rows = conn.execute(
        f"""SELECT DISTINCT c.page_start AS p FROM chunks c
            WHERE c.doc_id = %(doc)s AND c.review_en = 'pending' AND {FLAGGED_SQL}
            ORDER BY 1""",
        {"doc": doc_id, **_flag_params()},
    ).fetchall()
    return [r["p"] for r in rows]


def pending_pages(conn: psycopg.Connection, doc_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT page_start AS p FROM chunks WHERE doc_id=%s AND review_en='pending' "
        "ORDER BY 1",
        (doc_id,),
    ).fetchall()
    return [r["p"] for r in rows]


def chapters(conn: psycopg.Connection, doc_id: int, review_col: str = "review_en") -> list[dict]:
    if review_col not in ("review_en", "review_th"):
        raise ValueError(review_col)
    return conn.execute(
        f"""SELECT coalesce(section_path[1], '(front matter)') AS title,
                   min(page_start) AS first_page, count(*) AS chunks,
                   count(*) FILTER (WHERE {review_col} = 'pending') AS pending
            FROM chunks WHERE doc_id = %s GROUP BY 1 ORDER BY 2""",
        (doc_id,),
    ).fetchall()


# ----------------------------------------------------------------------- actions
def set_approved(conn: psycopg.Connection, chunk_id: int, approved: bool) -> None:
    c = get_chunk(conn, chunk_id)
    conn.execute(
        "UPDATE chunks SET review_en = %s, reviewed_at = CASE WHEN %s THEN now() END WHERE id = %s",
        ("approved" if approved else "pending", approved, chunk_id),
    )
    log(conn, c["doc_id"], "r1", "approve" if approved else "unapprove", f"chunk:{chunk_id}")


def set_embed(conn: psycopg.Connection, chunk_id: int, embed: bool) -> None:
    c = get_chunk(conn, chunk_id)
    conn.execute("UPDATE chunks SET embed = %s WHERE id = %s", (embed, chunk_id))
    log(conn, c["doc_id"], "r1", "include" if embed else "skip", f"chunk:{chunk_id}")


def set_kind(conn: psycopg.Connection, chunk_id: int, kind: str) -> None:
    if kind not in KINDS:
        raise ReviewError(f"Unknown kind: {kind}")
    c = get_chunk(conn, chunk_id)
    conn.execute("UPDATE chunks SET kind = %s WHERE id = %s", (kind, chunk_id))
    log(conn, c["doc_id"], "r1", "kind", f"chunk:{chunk_id}", old=c["kind"], new=kind)


def _add_flag(flags: list[str], flag: str) -> list[str]:
    return sorted(set(flags) | {flag})


def edit_text(conn: psycopg.Connection, chunk_id: int, text: str) -> None:
    c = get_chunk(conn, chunk_id)
    text = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()
    if not text:
        raise ReviewError("The text cannot be empty. Use Skip to leave a chunk out.")
    if text == c["text_en"]:
        return
    markup = retag(c["markup_en"], text) if c["markup_en"] else ""
    conn.execute(
        "UPDATE chunks SET text_en=%s, markup_en=%s, flags=%s WHERE id=%s",
        (text, markup, _add_flag(c["flags"], "edited"), chunk_id),
    )
    # Keep the block (what gets redrawn in Thai) in step when this chunk is its only chunk.
    if len(c["block_ids"]) == 1:
        bid = c["block_ids"][0]
        sharing = conn.execute(
            "SELECT count(*) AS n FROM chunks WHERE %s = ANY(block_ids) AND id <> %s",
            (bid, chunk_id),
        ).fetchone()["n"]
        if sharing == 0:
            conn.execute(
                "UPDATE blocks SET text_en=%s, markup_en=%s WHERE id=%s",
                (text, markup, bid),
            )
    log(conn, c["doc_id"], "r1", "edit", f"chunk:{chunk_id}", old=c["text_en"], new=text)


def merge_with_previous(conn: psycopg.Connection, chunk_id: int) -> int:
    """Append a chunk to the chunk before it (in reading order). Returns that chunk's id."""
    c = get_chunk(conn, chunk_id)
    prev = conn.execute(
        "SELECT * FROM chunks WHERE doc_id=%s AND (ord, part) < (%s, %s) "
        "ORDER BY ord DESC, part DESC LIMIT 1",
        (c["doc_id"], c["ord"], c["part"]),
    ).fetchone()
    if prev is None:
        raise ReviewError("This is the first chunk: there is nothing before it to merge with.")
    if (prev["kind"] == "code") != (c["kind"] == "code") or "table" in (prev["kind"], c["kind"]):
        raise ReviewError("Code and tables can only be merged with the same kind of chunk.")
    if c["kind"] == "code":
        text, markup = prev["text_en"] + "\n" + c["text_en"], ""
    else:
        text = join_blocks(prev["text_en"], c["text_en"], set())
        markup = join_blocks(prev["markup_en"], c["markup_en"], set()) if prev["markup_en"] else ""
    block_ids = prev["block_ids"] + [b for b in c["block_ids"] if b not in prev["block_ids"]]
    conn.execute(
        """UPDATE chunks SET text_en=%s, markup_en=%s, block_ids=%s,
                  page_start=LEAST(page_start, %s), page_end=GREATEST(page_end, %s),
                  embed = embed OR %s, review_en='pending', reviewed_at=NULL, flags=%s
           WHERE id=%s""",
        (
            text,
            markup,
            block_ids,
            c["page_start"],
            c["page_end"],
            c["embed"],
            _add_flag(sorted(set(prev["flags"]) | set(c["flags"])), "merged"),
            prev["id"],
        ),
    )
    conn.execute("DELETE FROM chunks WHERE id=%s", (chunk_id,))
    log(conn, c["doc_id"], "r1", "merge", f"chunk:{prev['id']}", merged=chunk_id)
    return prev["id"]


def split_chunk(conn: psycopg.Connection, chunk_id: int, at: int) -> int:
    """Split a chunk before piece number ``at`` (a block, or a sentence when the chunk
    has a single block). Returns the id of the new, second chunk."""
    c = get_chunk(conn, chunk_id)
    pieces = split_points(conn, c)
    if not pieces:
        raise ReviewError("This chunk has a single sentence (or is code/table): nothing to split.")
    if not 1 <= at < len(pieces):
        raise ReviewError("Choose a split point between two pieces.")
    if len(c["block_ids"]) > 1:
        return _split_blocks(conn, c, at)
    left_m, right_m = " ".join(pieces[:at]), " ".join(pieces[at:])
    plain = not c["markup_en"]
    left_t = left_m if plain else strip_markup(left_m)
    right_t = right_m if plain else strip_markup(right_m)
    # make room for the new part right after this one
    conn.execute(
        "UPDATE chunks SET part = -(part + 2) WHERE doc_id=%s AND ord=%s AND part > %s",
        (c["doc_id"], c["ord"], c["part"]),
    )
    conn.execute(
        "UPDATE chunks SET part = -part - 1 WHERE doc_id=%s AND ord=%s AND part < 0",
        (c["doc_id"], c["ord"]),
    )
    flags = _add_flag(c["flags"], "split_by_user")
    conn.execute(
        "UPDATE chunks SET text_en=%s, markup_en=%s, flags=%s, review_en='pending', "
        "reviewed_at=NULL WHERE id=%s",
        (left_t, "" if plain else left_m, flags, chunk_id),
    )
    new_id = _insert_like(
        conn,
        c,
        ord_=c["ord"],
        part=c["part"] + 1,
        text=right_t,
        markup="" if plain else right_m,
        block_ids=c["block_ids"],
        page_start=c["page_start"],
        page_end=c["page_end"],
        flags=flags,
    )
    log(conn, c["doc_id"], "r1", "split", f"chunk:{chunk_id}", at=at, new=new_id)
    return new_id


def _split_blocks(conn: psycopg.Connection, c: dict, at: int) -> int:
    rows = conn.execute(
        "SELECT id, ord, page_no, markup_en, text_en, style FROM blocks WHERE id = ANY(%s)",
        (c["block_ids"],),
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    ordered = [by_id[b] for b in c["block_ids"] if b in by_id]
    left, right = ordered[:at], ordered[at:]
    mcq = "mcq" in c["flags"]

    def compose(blocks: list[dict]) -> tuple[str, str]:
        text = markup = ""
        for i, b in enumerate(blocks):
            t = strip_markup(b["markup_en"]) if b["markup_en"] else b["text_en"]
            m = b["markup_en"] or t
            marker = (b["style"] or {}).get("marker")
            # options of a multiple-choice question keep their "a." / "b." marker,
            # also when they start the second half of a split
            option = mcq and marker and b["id"] != ordered[0]["id"]
            if option:
                t, m = f"{marker} {t}", f"{marker} {m}"
            if i == 0:
                text, markup = t, m
            elif option:
                text, markup = f"{text}\n{t}", f"{markup}\n{m}"
            else:
                text, markup = join_blocks(text, t, set()), join_blocks(markup, m, set())
        return text, markup

    lt, lm = compose(left)
    rt, rm = compose(right)
    flags = _add_flag(c["flags"], "split_by_user")
    conn.execute(
        """UPDATE chunks SET text_en=%s, markup_en=%s, block_ids=%s, page_start=%s, page_end=%s,
                  flags=%s, review_en='pending', reviewed_at=NULL WHERE id=%s""",
        (
            lt,
            lm if c["markup_en"] else "",
            [b["id"] for b in left],
            min(b["page_no"] for b in left),
            max(b["page_no"] for b in left),
            flags,
            c["id"],
        ),
    )
    ord_ = right[0]["ord"]
    taken = conn.execute(
        "SELECT coalesce(max(part) + 1, 0) AS p FROM chunks WHERE doc_id=%s AND ord=%s",
        (c["doc_id"], ord_),
    ).fetchone()["p"]
    new_id = _insert_like(
        conn,
        c,
        ord_=ord_,
        part=taken,
        text=rt,
        markup=rm if c["markup_en"] else "",
        block_ids=[b["id"] for b in right],
        page_start=min(b["page_no"] for b in right),
        page_end=max(b["page_no"] for b in right),
        flags=[f for f in flags if f != "mcq"] if mcq else flags,
    )
    log(conn, c["doc_id"], "r1", "split", f"chunk:{c['id']}", at=at, new=new_id, by="block")
    return new_id


def _insert_like(
    conn: psycopg.Connection,
    c: dict,
    *,
    ord_: int,
    part: int,
    text: str,
    markup: str,
    block_ids: list[int],
    page_start: int,
    page_end: int,
    flags: list[str],
) -> int:
    return conn.execute(
        """INSERT INTO chunks (doc_id, ord, part, kind, section_path, context_en, text_en,
                               markup_en, block_ids, page_start, page_end, list_level, embed,
                               flags)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (
            c["doc_id"],
            ord_,
            part,
            c["kind"],
            c["section_path"],
            c["context_en"],
            text,
            markup,
            block_ids,
            page_start,
            page_end,
            c["list_level"],
            c["embed"],
            flags,
        ),
    ).fetchone()["id"]


# ------------------------------------------------------------------ bulk approve
def approve_page(conn: psycopg.Connection, doc_id: int, page_no: int) -> int:
    n = conn.execute(
        "UPDATE chunks SET review_en='approved', reviewed_at=now() WHERE doc_id=%s "
        "AND %s BETWEEN page_start AND page_end AND review_en='pending'",
        (doc_id, page_no),
    ).rowcount
    log(conn, doc_id, "r1", "approve_page", f"page:{page_no}", count=n)
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
        "UPDATE chunks SET review_en='approved', reviewed_at=now() WHERE doc_id=%s "
        "AND coalesce(section_path[1], '') = %s AND review_en='pending'",
        (doc_id, row["title"]),
    ).rowcount
    log(conn, doc_id, "r1", "approve_chapter", f"page:{page_no}", chapter=row["title"], count=n)
    return row["title"] or "(front matter)", n


def approve_all(conn: psycopg.Connection, doc_id: int) -> int:
    n = conn.execute(
        "UPDATE chunks SET review_en='approved', reviewed_at=now() WHERE doc_id=%s "
        "AND review_en='pending'",
        (doc_id,),
    ).rowcount
    log(conn, doc_id, "r1", "approve_all", f"doc:{doc_id}", count=n)
    return n
