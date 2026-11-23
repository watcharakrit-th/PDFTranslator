"""The Thai context of each chunk, built like the English one (see pdftr.ingest.cutter).

The English context has up to four lines: ``Section:`` (the section path), ``Intro:``
(the sentence that introduces a list), ``Parent item:`` and ``Question:``. The Thai
context has the same lines with Thai labels and Thai values: section titles from the
translated headings and bookmarks, the rest from the Thai of the chunks they quote.
A value with no Thai yet stays English (BGE-M3 is multilingual, so it still helps).
"""

from __future__ import annotations

import re

import psycopg

from pdftr.translate.runner import split_markup
from pdftr.translate.titles import heading_thai, norm_title

LABELS_TH = {
    "Section": "หัวข้อ",
    "Intro": "ข้อความนำ",
    "Parent item": "รายการหลัก",
    "Question": "คำถาม",
}
INTRO_LOOKBACK = 30  # chunks to search back for the paragraph that introduces a list


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def section_titles(conn: psycopg.Connection, doc_id: int) -> dict[str, str]:
    """Normalised English section title -> Thai, from headings and bookmarks."""
    titles = heading_thai(conn, doc_id)
    for e in conn.execute(
        "SELECT title_en, title_th FROM outline_entries WHERE doc_id=%s "
        "AND coalesce(title_th, '') <> ''",
        (doc_id,),
    ).fetchall():
        titles.setdefault(norm_title(e["title_en"]), e["title_th"])
    return titles


def thai_contexts(conn: psycopg.Connection, doc_id: int) -> dict[int, str]:
    """chunk id -> Thai context, for every chunk of the document."""
    chunks = conn.execute(
        """SELECT id, section_path, context_en, text_en, text_th, flags FROM chunks
           WHERE doc_id=%s ORDER BY ord, part""",
        (doc_id,),
    ).fetchall()
    titles = section_titles(conn, doc_id)
    by_text: dict[str, str] = {}  # English text -> Thai (for parent items and questions)
    for c in chunks:
        if not c["text_th"]:
            continue
        by_text.setdefault(_norm(c["text_en"]), c["text_th"])
        if "mcq" in c["flags"]:  # the question line of a multiple-choice chunk
            en, th = c["text_en"].split("\n")[0], c["text_th"].split("\n")[0]
            by_text.setdefault(_norm(en), th)
    contexts: dict[int, str] = {}
    for i, c in enumerate(chunks):
        lines = []
        if c["section_path"]:
            path = [titles.get(norm_title(t)) or t for t in c["section_path"]]
            lines.append(f"{LABELS_TH['Section']}: " + " > ".join(path))
        for line in (c["context_en"] or "").split("\n"):
            label, _, value = line.partition(": ")
            if label not in LABELS_TH or label == "Section" or not value:
                continue
            if label == "Intro":
                thai = _intro_thai(value, chunks, i)
            else:
                thai = by_text.get(_norm(value))
            lines.append(f"{LABELS_TH[label]}: {thai or value}")
        contexts[c["id"]] = "\n".join(lines)
    return contexts


def _intro_thai(intro: str, chunks: list[dict], i: int) -> str | None:
    """Thai of a list's intro sentence: the end of the paragraph that introduces the list,
    cut at a space in proportion to where the sentence starts in the English."""
    intro = _norm(intro)
    for c in reversed(chunks[max(0, i - INTRO_LOOKBACK) : i]):
        english = _norm(c["text_en"])
        if not c["text_th"] or not english.endswith(intro):
            continue
        before = len(english) - len(intro)
        if before <= 0:
            return c["text_th"]
        return split_markup(c["text_th"], [before, len(intro)])[-1] or c["text_th"]
    return None
