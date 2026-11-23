"""R2 (English term list) and R3 (Thai term list)."""

from __future__ import annotations

import re

import psycopg

from pdftr.glossary import DECISIONS, load_seed
from pdftr.ingest.terms import normalize
from pdftr.ingest.textutil import split_sentences
from pdftr.review import ReviewError
from pdftr.review.chunks import log

KEY_SOURCES = "ARRAY['styled','key_terms_list']::text[]"
TABS = {
    # tab: (label, SQL condition); every term appears in exactly one of the first four
    "key": ("Book key terms", f"dt.sources && {KEY_SOURCES} AND t.kind <> 'code'"),
    "acronym": ("Acronyms", f"t.kind = 'acronym' AND NOT dt.sources && {KEY_SOURCES}"),
    "phrase": (
        "Phrases (maybe)",
        f"t.kind = 'term' AND NOT dt.sources && {KEY_SOURCES}",
    ),
    "code": ("Commands", "t.kind = 'code'"),
    "all": ("All", "TRUE"),
}
DECISION_LABELS = {"keep_en": "Keep English", "translate": "Translate", "ignore": "Not a term"}
PAGE_SIZE = 150


def _where(tab: str, step: str, status: str, q: str) -> tuple[str, dict]:
    if tab not in TABS:
        tab = "all"
    parts = [TABS[tab][1]]
    params: dict = {}
    if step == "r3":
        parts.append("t.decision = 'translate'")
        if status == "missing":
            parts.append("coalesce(t.term_th, '') = ''")
        elif status == "pending":
            parts.append("t.review_th = 'pending'")
    elif status == "pending":
        parts.append("t.review_en = 'pending'")
    if q:
        parts.append("(t.term_en ILIKE %(q)s OR coalesce(t.term_th, '') ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    return " AND ".join(f"({p})" for p in parts), params


def term_rows(
    conn: psycopg.Connection,
    doc_id: int,
    *,
    tab: str = "key",
    step: str = "r2",
    status: str = "pending",
    q: str = "",
    page: int = 1,
) -> dict:
    where, params = _where(tab, step, status, q)
    params |= {"doc": doc_id, "limit": PAGE_SIZE, "offset": (max(page, 1) - 1) * PAGE_SIZE}
    total = conn.execute(
        f"SELECT count(*) AS n FROM doc_terms dt JOIN terms t ON t.id = dt.term_id "
        f"WHERE dt.doc_id = %(doc)s AND {where}",
        params,
    ).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT t.*, dt.freq, dt.first_page, dt.sources, dt.example
            FROM doc_terms dt JOIN terms t ON t.id = dt.term_id
            WHERE dt.doc_id = %(doc)s AND {where}
            ORDER BY dt.freq DESC, t.term_en LIMIT %(limit)s OFFSET %(offset)s""",
        params,
    ).fetchall()
    return {"rows": rows, "total": total, "page": page, "pages": max(1, -(-total // PAGE_SIZE))}


def tab_counts(conn: psycopg.Connection, doc_id: int, step: str = "r2") -> dict:
    counts = {}
    for tab in TABS:
        where, params = _where(tab, step, "all", "")
        review_col = "review_th" if step == "r3" else "review_en"
        row = conn.execute(
            f"""SELECT count(*) AS total,
                       count(*) FILTER (WHERE t.{review_col} = 'pending') AS pending
                FROM doc_terms dt JOIN terms t ON t.id = dt.term_id
                WHERE dt.doc_id = %(doc)s AND {where}""",
            params | {"doc": doc_id},
        ).fetchone()
        counts[tab] = row
    return counts


def get_term(conn: psycopg.Connection, term_id: int) -> dict:
    row = conn.execute("SELECT * FROM terms WHERE id=%s", (term_id,)).fetchone()
    if row is None:
        raise ReviewError(f"Term {term_id} not found.")
    return row


def term_row(conn: psycopg.Connection, doc_id: int, term_id: int) -> dict:
    row = conn.execute(
        """SELECT t.*, dt.freq, dt.first_page, dt.sources, dt.example
           FROM terms t LEFT JOIN doc_terms dt ON dt.term_id = t.id AND dt.doc_id = %s
           WHERE t.id = %s""",
        (doc_id, term_id),
    ).fetchone()
    if row is None:
        raise ReviewError(f"Term {term_id} not found.")
    return row


# ------------------------------------------------------------------------ R2
def set_decision(conn: psycopg.Connection, doc_id: int, term_id: int, decision: str) -> None:
    if decision not in DECISIONS:
        raise ReviewError(f"Unknown decision: {decision}")
    t = get_term(conn, term_id)
    term_th, th_source = t["term_th"], t["th_source"]
    if decision == "translate" and not term_th and t["norm"] in load_seed():
        term_th, th_source = load_seed()[t["norm"]], "seed"
    conn.execute(
        """UPDATE terms SET decision=%s, term_th=%s, th_source=%s, review_en='approved',
                  reviewed_at=now(), updated_at=now() WHERE id=%s""",
        (decision, term_th, th_source, term_id),
    )
    log(conn, doc_id, "r2", "decision", f"term:{term_id}", old=t["decision"], new=decision)


def rename(conn: psycopg.Connection, doc_id: int, term_id: int, term_en: str) -> None:
    term_en = re.sub(r"\s+", " ", term_en).strip()
    if not term_en:
        raise ReviewError("The term cannot be empty.")
    t = get_term(conn, term_id)
    conn.execute(
        "UPDATE terms SET term_en=%s, review_en='approved', reviewed_at=now(), updated_at=now() "
        "WHERE id=%s",
        (term_en, term_id),
    )
    log(conn, doc_id, "r2", "rename", f"term:{term_id}", old=t["term_en"], new=term_en)


def approve_en(
    conn: psycopg.Connection, doc_id: int, term_ids: list[int], approved: bool = True
) -> int:
    n = conn.execute(
        "UPDATE terms SET review_en=%s, reviewed_at=CASE WHEN %s THEN now() END, "
        "updated_at=now() WHERE id = ANY(%s)",
        ("approved" if approved else "pending", approved, term_ids),
    ).rowcount
    log(
        conn,
        doc_id,
        "r2",
        "approve" if approved else "unapprove",
        f"terms:{len(term_ids)}",
        ids=term_ids[:500],
    )
    return n


def approve_all_en(conn: psycopg.Connection, doc_id: int) -> int:
    """R2: approve every pending term of the document, in every tab, with the decision
    it has now (keep English / translate / not a term)."""
    n = conn.execute(
        """UPDATE terms t SET review_en='approved', reviewed_at=now(), updated_at=now()
           FROM doc_terms dt
           WHERE dt.term_id = t.id AND dt.doc_id=%s AND t.review_en='pending'""",
        (doc_id,),
    ).rowcount
    log(conn, doc_id, "r2", "approve_all", f"doc:{doc_id}", count=n)
    return n


def add_term(conn: psycopg.Connection, doc_id: int, term_en: str, decision: str) -> int:
    term_en = re.sub(r"\s+", " ", term_en).strip()
    if not term_en:
        raise ReviewError("Type the term to add.")
    if decision not in DECISIONS:
        raise ReviewError(f"Unknown decision: {decision}")
    norm = normalize(term_en)
    seed_th = load_seed().get(norm) if decision == "translate" else None
    term_id = conn.execute(
        """INSERT INTO terms (norm, term_en, kind, decision, term_th, th_source, source,
                              review_en, reviewed_at)
           VALUES (%s, %s, %s, %s, %s, %s, 'user', 'approved', now())
           ON CONFLICT (norm) DO UPDATE SET term_en = EXCLUDED.term_en,
             decision = EXCLUDED.decision, review_en = 'approved', reviewed_at = now(),
             term_th = coalesce(terms.term_th, EXCLUDED.term_th),
             th_source = coalesce(terms.th_source, EXCLUDED.th_source), updated_at = now()
           RETURNING id""",
        (
            norm,
            term_en,
            "acronym" if term_en.isupper() else "term",
            decision,
            seed_th,
            "seed" if seed_th else None,
        ),
    ).fetchone()["id"]
    freq, first_page, example = _occurrences(conn, doc_id, norm)
    conn.execute(
        """INSERT INTO doc_terms (doc_id, term_id, freq, first_page, sources, example)
           VALUES (%s, %s, %s, %s, '{user}', %s)
           ON CONFLICT (doc_id, term_id) DO UPDATE SET
             sources = (SELECT array_agg(DISTINCT s) FROM unnest(doc_terms.sources || '{user}') s)""",
        (doc_id, term_id, freq, first_page, example),
    )
    log(conn, doc_id, "r2", "add", f"term:{term_id}", term=term_en, decision=decision)
    return term_id


def _occurrences(
    conn: psycopg.Connection, doc_id: int, norm: str
) -> tuple[int, int | None, str | None]:
    words = [re.escape(w) for w in norm.split(" ")]
    words[-1] += r"(?:s|es)?"
    pattern = re.compile(r"\b" + r"[\s-]+".join(words) + r"\b", re.IGNORECASE)
    freq, first_page, example = 0, None, None
    for r in conn.execute(
        "SELECT page_start, text_en FROM chunks WHERE doc_id=%s ORDER BY ord, part", (doc_id,)
    ):
        hits = len(pattern.findall(r["text_en"]))
        if hits:
            freq += hits
            if first_page is None:
                first_page = r["page_start"]
                example = next(
                    (s for s in split_sentences(r["text_en"]) if pattern.search(s)), None
                )
    return freq, first_page, example


# ------------------------------------------------------------------------ R3
def set_thai(conn: psycopg.Connection, doc_id: int, term_id: int, term_th: str) -> None:
    term_th = re.sub(r"\s+", " ", term_th).strip()
    t = get_term(conn, term_id)
    conn.execute(
        """UPDATE terms SET term_th=%s, th_source=%s, review_th=%s,
                  reviewed_at=now(), updated_at=now() WHERE id=%s""",
        (
            term_th or None,
            "user" if term_th else None,
            "approved" if term_th else "pending",
            term_id,
        ),
    )
    log(conn, doc_id, "r3", "thai", f"term:{term_id}", old=t["term_th"], new=term_th)


def approve_th(
    conn: psycopg.Connection, doc_id: int, term_ids: list[int], approved: bool = True
) -> int:
    n = conn.execute(
        """UPDATE terms SET review_th=%s, reviewed_at=now(), updated_at=now()
           WHERE id = ANY(%s) AND (NOT %s OR coalesce(term_th, '') <> '')""",
        ("approved" if approved else "pending", term_ids, approved),
    ).rowcount
    log(
        conn,
        doc_id,
        "r3",
        "approve" if approved else "unapprove",
        f"terms:{len(term_ids)}",
        ids=term_ids[:500],
    )
    return n


def approve_all_th(conn: psycopg.Connection, doc_id: int) -> tuple[int, int]:
    """R3: approve every pending Thai term of the document that has its Thai filled in.
    Returns (approved, still without Thai): those stay pending until they get Thai."""
    n = conn.execute(
        """UPDATE terms t SET review_th='approved', reviewed_at=now(), updated_at=now()
           FROM doc_terms dt
           WHERE dt.term_id = t.id AND dt.doc_id=%s AND t.decision='translate'
             AND t.review_th='pending' AND coalesce(t.term_th, '') <> ''""",
        (doc_id,),
    ).rowcount
    missing = conn.execute(
        """SELECT count(*) AS n FROM doc_terms dt JOIN terms t ON t.id = dt.term_id
           WHERE dt.doc_id=%s AND t.decision='translate' AND coalesce(t.term_th, '') = ''""",
        (doc_id,),
    ).fetchone()["n"]
    log(conn, doc_id, "r3", "approve_all", f"doc:{doc_id}", count=n, missing=missing)
    return n, missing


def suggest_thai(
    conn: psycopg.Connection, doc_id: int, engine, limit: int = 100
) -> tuple[int, str | None]:
    """Machine suggestions for terms to translate that have no Thai yet.

    They are marked as machine-made and stay pending: you still approve each one.
    Returns (terms suggested, reason if the GPU guard stopped the series early).
    """
    from pdftr.gpu import GpuTooHot
    from pdftr.translate.rules import STYLE_RULE

    rows = conn.execute(
        """SELECT t.id, t.term_en FROM doc_terms dt JOIN terms t ON t.id = dt.term_id
           WHERE dt.doc_id=%s AND t.decision='translate' AND coalesce(t.term_th, '') = ''
           ORDER BY dt.freq DESC LIMIT %s""",
        (doc_id, limit),
    ).fetchall()
    rules = [STYLE_RULE, "This is a technical term: return only its Thai equivalent."]
    done, stopped = 0, None
    for r in rows:
        try:
            answer = engine.translate(r["term_en"], rules).text
        except GpuTooHot as exc:
            stopped = str(exc)
            break
        thai = (answer.splitlines() or [""])[0].strip(" .\"'")
        if thai:
            conn.execute(
                "UPDATE terms SET term_th=%s, th_source='mt', review_th='pending', "
                "updated_at=now() WHERE id=%s",
                (thai, r["id"]),
            )
        done += 1
    log(conn, doc_id, "r3", "suggest", f"terms:{done}")
    return done, stopped
