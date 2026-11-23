"""Embed a document's reviewed chunks into pgvector (after R5).

- English: every chunk to embed (cutter rules and R1) that R1 approved, except tables.
- Thai: the same chunks once R4 approved their Thai, except code (embedded once, in
  English, since it is never translated).
- The embedded text is context + chunk; its hash (with the model name) is stored, so a
  re-run embeds only new or changed text and clears vectors of chunks no longer included.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

import psycopg

from pdftr.embed.context import thai_contexts
from pdftr.embed.model import Embedder

GROUP = 64  # texts per database round (the model batches inside)
EMBEDDED, TO_EMBED, CHANGED = "embedded", "to embed", "changed"


@dataclass
class ChunkPlan:
    id: int
    page: int
    chapter: str
    kind: str
    en_text: str  # exactly what is embedded: context + chunk
    th_text: str
    en: str  # embedded | to embed | changed | not embedded
    th: str
    en_key: str
    th_key: str
    en_reason: str = ""  # why not embedded
    th_reason: str = ""

    def todo(self, lang: str) -> bool:
        return getattr(self, lang) in (TO_EMBED, CHANGED)


def embed_text(context: str | None, text: str) -> str:
    return f"{context}\n{text}" if context else text


def embed_key(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()[:20]


def _reasons(c: dict) -> tuple[str, str]:
    """Why a chunk is not embedded in English / Thai ("" when it is)."""
    if c["kind"] == "table":
        en = "table: kept as text"
    elif not c["embed"]:
        en = "kept out (R1, or a Key Terms list)"
    elif c["review_en"] != "approved":
        en = "not approved in R1"
    else:
        en = ""
    if en:
        return en, en
    if c["kind"] == "code":
        return "", "code: embedded once, in English"
    if not c["text_th"]:
        return "", "no Thai yet"
    if c["review_th"] != "approved":
        return "", "Thai not approved in R4"
    return "", ""


def plan(
    conn: psycopg.Connection, doc_id: int, model: str, *, force: bool = False
) -> list[ChunkPlan]:
    """The embedding state of every chunk. Stores the Thai contexts as a side effect."""
    contexts = thai_contexts(conn, doc_id)
    rows = conn.execute(
        """SELECT id, page_start, section_path, kind, embed, review_en, review_th,
                  context_en, context_th, text_en, text_th, embed_key_en, embed_key_th,
                  embedding_en IS NOT NULL AS has_en, embedding_th IS NOT NULL AS has_th
           FROM chunks WHERE doc_id=%s ORDER BY ord, part""",
        (doc_id,),
    ).fetchall()
    plans = []
    for c in rows:
        context_th = contexts.get(c["id"], "")
        if context_th != (c["context_th"] or ""):
            conn.execute("UPDATE chunks SET context_th=%s WHERE id=%s", (context_th, c["id"]))
        en_reason, th_reason = _reasons(c)
        texts = {
            "en": embed_text(c["context_en"], c["text_en"]),
            "th": embed_text(context_th, c["text_th"] or ""),
        }
        states, keys = {}, {}
        for lang, reason in (("en", en_reason), ("th", th_reason)):
            keys[lang] = embed_key(model, texts[lang])
            if reason:
                states[lang] = "not embedded"
            elif not c[f"has_{lang}"]:
                states[lang] = TO_EMBED
            elif c[f"embed_key_{lang}"] != keys[lang] or force:
                states[lang] = CHANGED
            else:
                states[lang] = EMBEDDED
        plans.append(
            ChunkPlan(
                id=c["id"],
                page=c["page_start"],
                chapter=(c["section_path"] or [""])[0],
                kind=c["kind"],
                en_text=texts["en"],
                th_text=texts["th"],
                en=states["en"],
                th=states["th"],
                en_key=keys["en"],
                th_key=keys["th"],
                en_reason=en_reason,
                th_reason=th_reason,
            )
        )
    return plans


def summary(plans: list[ChunkPlan]) -> dict:
    out = {"chunks": len(plans)}
    for lang in ("en", "th"):
        states = [getattr(p, lang) for p in plans]
        out[f"{lang}_total"] = sum(s != "not embedded" for s in states)
        out[f"{lang}_done"] = states.count(EMBEDDED)
        out[f"{lang}_todo"] = states.count(TO_EMBED) + states.count(CHANGED)
    out["not_embedded"] = sum(p.en == "not embedded" for p in plans)
    return out


def embed_document(
    conn: psycopg.Connection,
    doc_id: int,
    embedder: Embedder,
    *,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> dict:
    from pgvector.psycopg import register_vector

    register_vector(conn)
    started = time.monotonic()
    plans = plan(conn, doc_id, embedder.name, force=force)
    cleared = 0
    for lang in ("en", "th"):  # vectors of chunks that are no longer included
        ids = [p.id for p in plans if getattr(p, lang) == "not embedded"]
        cleared += conn.execute(
            f"UPDATE chunks SET embedding_{lang}=NULL, embed_key_{lang}=NULL "
            f"WHERE id = ANY(%s) AND embedding_{lang} IS NOT NULL",
            (ids,),
        ).rowcount
    conn.commit()
    todo = [(p, lang) for p in plans for lang in ("en", "th") if p.todo(lang)]
    counts = summary(plans)
    log(
        f"{counts['en_total']} English and {counts['th_total']} Thai texts in all: "
        f"{len(todo)} to embed now, {counts['en_done'] + counts['th_done']} up to date"
    )
    for start in range(0, len(todo), GROUP):
        group = todo[start : start + GROUP]
        vectors = embedder.encode([p.en_text if lang == "en" else p.th_text for p, lang in group])
        for (p, lang), vector in zip(group, vectors, strict=True):
            conn.execute(
                f"UPDATE chunks SET embedding_{lang}=%s, embed_key_{lang}=%s, embedded_at=now() "
                "WHERE id=%s",
                (vector, p.en_key if lang == "en" else p.th_key, p.id),
            )
        conn.commit()
        log(f"  {start + len(group)}/{len(todo)} embedded")
    return {
        "english": counts["en_total"],
        "thai": counts["th_total"],
        "embedded_now": len(todo),
        "up_to_date": counts["en_done"] + counts["th_done"],
        "cleared": cleared,
        "not_embedded": counts["not_embedded"],
        "seconds": round(time.monotonic() - started, 1),
    }
