"""Titles outside the body text: the contents pages and the PDF outline (bookmarks).

They reuse the Thai of the matching heading where there is one, so a chapter has the
same Thai title on its first page, in the contents and in the bookmarks. Titles
without a matching heading are translated like any other short text.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable

import psycopg

from pdftr.ingest.textutil import strip_markup


def norm_title(text: str) -> str:
    """Compare titles loosely: case, spacing and trailing punctuation don't matter."""
    return re.sub(r"\s+", " ", text or "").strip(" .:-–—").lower()


def heading_thai(conn: psycopg.Connection, doc_id: int) -> dict[str, str]:
    """Translated headings of the document: normalised English -> Thai (plain text)."""
    rows = conn.execute(
        """SELECT text_en, markup_th FROM blocks
           WHERE doc_id=%s AND role='heading' AND coalesce(markup_th, '') <> ''
           ORDER BY ord""",
        (doc_id,),
    ).fetchall()
    titles: dict[str, str] = {}
    for r in rows:
        titles.setdefault(norm_title(r["text_en"]), strip_markup(r["markup_th"]).strip())
    return titles


def load_outline(conn: psycopg.Connection, doc_id: int) -> list[dict]:
    """The outline entries for the document's pages, read from the PDF the first time."""
    rows = conn.execute(
        "SELECT * FROM outline_entries WHERE doc_id=%s ORDER BY seq", (doc_id,)
    ).fetchall()
    if rows:
        return rows
    import pymupdf

    doc = conn.execute("SELECT file_path FROM documents WHERE id=%s", (doc_id,)).fetchone()
    pages = {
        r["page_no"]
        for r in conn.execute("SELECT page_no FROM pages WHERE doc_id=%s", (doc_id,)).fetchall()
    }
    with pymupdf.open(doc["file_path"]) as pdf:
        toc = pdf.get_toc(simple=True)
    seq = 0
    for level, title, page in toc:
        if page in pages and title.strip():
            seq += 1
            conn.execute(
                """INSERT INTO outline_entries (doc_id, seq, level, page_no, title_en)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT (doc_id, seq) DO NOTHING""",
                (doc_id, seq, level, page, title.strip()),
            )
    conn.commit()
    return conn.execute(
        "SELECT * FROM outline_entries WHERE doc_id=%s ORDER BY seq", (doc_id,)
    ).fetchall()


def translate_outline(
    conn: psycopg.Connection,
    doc_id: int,
    translate: Callable[[str], tuple[str, str, tuple[str, ...]]],
    *,
    first: int = 1,
    last: int = 10**9,
    force: bool = False,
    titles: dict[str, str] | None = None,
) -> int:
    """Thai titles for the outline entries on pages first..last.

    ``translate(markup)`` returns (thai markup, key, flags) for titles that have no
    matching heading. Your own edits (source 'human') are never replaced.
    """
    titles = titles if titles is not None else heading_thai(conn, doc_id)
    count = 0
    for e in load_outline(conn, doc_id):
        if not first <= e["page_no"] <= last or e["tr_source"] == "human":
            continue
        if e["title_th"] and not force:
            continue
        same = titles.get(norm_title(e["title_en"]))
        if same:
            thai, source, key, flags = same, "heading", None, ()
        else:
            markup, key, flags = translate(html.escape(e["title_en"], quote=False))
            thai, source = strip_markup(markup or "").strip(), "mt"
        conn.execute(
            """UPDATE outline_entries SET title_th=%s, tr_source=%s, tr_key=%s, tr_flags=%s
               WHERE id=%s""",
            (thai, source, key, list(flags), e["id"]),
        )
        conn.commit()
        count += 1
    return count


def set_outline_thai(conn: psycopg.Connection, entry_id: int, thai: str) -> None:
    conn.execute(
        """UPDATE outline_entries SET title_th=%s, tr_source='human', tr_flags='{}'
           WHERE id=%s""",
        (thai.strip(), entry_id),
    )
