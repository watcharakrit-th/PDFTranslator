"""The review gates: each step unlocks only when the previous one is complete."""

from __future__ import annotations

import psycopg

from pdftr.review import ReviewError
from pdftr.review.chunks import log

# document.stage values, in order
STAGES = [
    "r1_chunks_en",
    "r2_terms_en",
    "r3_terms_th",
    "translate",
    "r4_chunks_th",
    "r5_embed",
    "done",
]
STEPS = [
    # step, stage it belongs to, label, available in this build
    ("r1", "r1_chunks_en", "R1 English chunks", True),
    ("r2", "r2_terms_en", "R2 English terms", True),
    ("r3", "r3_terms_th", "R3 Thai terms", True),
    ("r4", "r4_chunks_th", "R4 Thai chunks", True),
    ("r5", "r5_embed", "R5 Embedding", True),
]
STEP_STAGE = {s: stage for s, stage, _, _ in STEPS}


def step_states(stage: str) -> list[dict]:
    current = STAGES.index(stage) if stage in STAGES else 0
    out = []
    for step, st, label, built in STEPS:
        i = STAGES.index(st)
        state = "done" if i < current else "current" if i == current else "locked"
        if stage == "translate" and st == "r4_chunks_th":
            state = "current"  # R4 opens while the translation runs (it shows how far it is)
        if not built and state != "done":
            state = "later"
        out.append({"step": step, "label": label, "state": state})
    return out


def blockers(conn: psycopg.Connection, doc_id: int, step: str) -> str | None:
    """Why a step cannot be completed yet (None when it can)."""
    if step == "r1":
        n = conn.execute(
            "SELECT count(*) AS n FROM chunks WHERE doc_id=%s AND review_en='pending'", (doc_id,)
        ).fetchone()["n"]
        return f"{n} chunks still pending" if n else None
    if step == "r2":
        n = conn.execute(
            """SELECT count(*) AS n FROM doc_terms dt JOIN terms t ON t.id = dt.term_id
               WHERE dt.doc_id=%s AND t.review_en='pending'""",
            (doc_id,),
        ).fetchone()["n"]
        return f"{n} terms still pending" if n else None
    if step == "r3":
        row = conn.execute(
            """SELECT count(*) FILTER (WHERE coalesce(t.term_th, '') = '') AS missing,
                      count(*) FILTER (WHERE t.review_th = 'pending') AS pending
               FROM doc_terms dt JOIN terms t ON t.id = dt.term_id
               WHERE dt.doc_id=%s AND t.decision='translate'""",
            (doc_id,),
        ).fetchone()
        if row["missing"]:
            return f"{row['missing']} terms to translate have no Thai yet"
        return f"{row['pending']} Thai terms still pending" if row["pending"] else None
    if step == "r4":
        row = conn.execute(
            """SELECT count(*) FILTER (WHERE text_th IS NULL) AS missing,
                      count(*) FILTER (WHERE review_th = 'pending') AS pending
               FROM chunks WHERE doc_id=%s""",
            (doc_id,),
        ).fetchone()
        if row["missing"]:
            return f"{row['missing']} chunks are not translated yet"
        return f"{row['pending']} Thai chunks still pending" if row["pending"] else None
    if step == "r5":
        n = conn.execute(
            """SELECT count(*) AS n FROM chunks WHERE doc_id=%s AND embed AND kind <> 'table'
               AND review_en = 'approved'""",
            (doc_id,),
        ).fetchone()["n"]
        return None if n else "there is nothing to embed"
    raise ReviewError(f"Unknown step {step}")


def complete(conn: psycopg.Connection, doc_id: int, step: str) -> str:
    """Finish a step and unlock the next. Returns the new stage."""
    doc = conn.execute("SELECT stage FROM documents WHERE id=%s", (doc_id,)).fetchone()
    if doc is None:
        raise ReviewError(f"Document {doc_id} not found.")
    stage = STEP_STAGE.get(step)
    if step == "r4" and doc["stage"] == "translate":
        raise ReviewError(
            f"The translation has not finished: use Translate all, or run `pdftr translate {doc_id}`."
        )
    if doc["stage"] != stage:
        raise ReviewError(f"{step.upper()} is not the current step.")
    reason = blockers(conn, doc_id, step)
    if reason:
        raise ReviewError(f"Cannot complete {step.upper()} yet: {reason}.")
    new_stage = STAGES[STAGES.index(stage) + 1]
    conn.execute("UPDATE documents SET stage=%s, updated_at=now() WHERE id=%s", (new_stage, doc_id))
    if step == "r5":  # the embedding may start now (pdftr embed)
        conn.execute("UPDATE documents SET embed_approved_at=now() WHERE id=%s", (doc_id,))
    log(conn, doc_id, step, "complete", f"doc:{doc_id}", stage=new_stage)
    return new_stage
