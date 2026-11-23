"""Search the embedded chunks in Thai or English.

The query is embedded once and compared with both the English and the Thai vectors
(each through its HNSW index); a chunk's score is its best match in either language.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from pdftr.embed.model import Embedder


@dataclass
class Hit:
    chunk_id: int
    doc_id: int
    title: str
    page_start: int
    page_end: int
    label: str  # printed page number
    section_path: list[str]
    kind: str
    text_en: str
    text_th: str | None
    score: float  # cosine similarity, 1 = identical
    lang: str  # which language matched best


def search(
    conn: psycopg.Connection,
    embedder: Embedder,
    query: str,
    *,
    doc_id: int | None = None,
    top: int = 10,
    langs: tuple[str, ...] = ("en", "th"),
) -> list[Hit]:
    from pgvector.psycopg import register_vector

    register_vector(conn)
    anything = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM chunks WHERE (embedding_en IS NOT NULL "
        "OR embedding_th IS NOT NULL) AND (%(doc)s::int IS NULL OR doc_id = %(doc)s)) AS yes",
        {"doc": doc_id},
    ).fetchone()["yes"]
    if not anything or not query.strip():
        return []  # don't load the model for nothing
    vector = embedder.encode([query])[0]
    best: dict[int, tuple[float, str]] = {}
    with conn.transaction():
        conn.execute("SET LOCAL hnsw.ef_search = 100")
        conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")  # keeps filters accurate
        for lang in langs:
            column = f"embedding_{lang}"
            where = f"{column} IS NOT NULL" + (" AND doc_id = %(doc)s" if doc_id else "")
            rows = conn.execute(
                f"SELECT id, {column} <=> %(q)s AS dist FROM chunks WHERE {where} "
                f"ORDER BY {column} <=> %(q)s LIMIT %(k)s",
                {"q": vector, "doc": doc_id, "k": top * 3},
            ).fetchall()
            for r in rows:
                if r["id"] not in best or r["dist"] < best[r["id"]][0]:
                    best[r["id"]] = (r["dist"], lang)
    ranked = sorted(best.items(), key=lambda kv: kv[1][0])[:top]
    if not ranked:
        return []
    rows = {
        r["id"]: r
        for r in conn.execute(
            """SELECT c.id, c.doc_id, c.page_start, c.page_end, c.section_path, c.kind,
                      c.text_en, c.text_th, d.title, coalesce(p.label, c.page_start::text) AS label
               FROM chunks c JOIN documents d ON d.id = c.doc_id
                    LEFT JOIN pages p ON p.doc_id = c.doc_id AND p.page_no = c.page_start
               WHERE c.id = ANY(%s)""",
            ([cid for cid, _ in ranked],),
        ).fetchall()
    }
    return [
        Hit(
            chunk_id=cid,
            doc_id=rows[cid]["doc_id"],
            title=rows[cid]["title"] or "",
            page_start=rows[cid]["page_start"],
            page_end=rows[cid]["page_end"],
            label=rows[cid]["label"],
            section_path=list(rows[cid]["section_path"] or []),
            kind=rows[cid]["kind"],
            text_en=rows[cid]["text_en"],
            text_th=rows[cid]["text_th"],
            score=round(1 - dist, 3),
            lang=lang,
        )
        for cid, (dist, lang) in ranked
        if cid in rows
    ]
