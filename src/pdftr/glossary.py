"""The shared glossary: seed defaults (policy b), CSV import/export."""

from __future__ import annotations

import csv
import io
from functools import lru_cache
from pathlib import Path

import psycopg

from pdftr.ingest.terms import normalize

SEED_PATH = Path(__file__).parent / "data" / "seed_glossary_th.csv"
DECISIONS = ("keep_en", "translate", "ignore")
CSV_FIELDS = ["term_en", "decision", "term_th", "kind", "note"]


@lru_cache(maxsize=1)
def load_seed() -> dict[str, str]:
    """normalized English term -> established Thai term."""
    with open(SEED_PATH, encoding="utf-8", newline="") as f:
        return {normalize(row["term_en"]): row["term_th"].strip() for row in csv.DictReader(f)}


def default_for(norm: str, kind: str, sources: set[str]) -> dict:
    """First-draft decision for a newly collected term (policy b).

    - commands are code: English, no review needed
    - a term with an established Thai equivalent (seed list): translate
    - acronyms and other specialized terms: keep English
    - phrases found only by frequency are probably not terms: ignore
    """
    if kind == "code":
        return {"decision": "keep_en", "review_en": "approved", "term_th": None, "th_source": None}
    seed = load_seed()
    if norm in seed:
        return {
            "decision": "translate",
            "review_en": "pending",
            "term_th": seed[norm],
            "th_source": "seed",
        }
    if sources and sources <= {"ngram"}:
        return {"decision": "ignore", "review_en": "pending", "term_th": None, "th_source": None}
    return {"decision": "keep_en", "review_en": "pending", "term_th": None, "th_source": None}


def apply_seed(conn: psycopg.Connection) -> int:
    """Re-apply defaults to collected terms nobody has reviewed yet."""
    rows = conn.execute(
        """SELECT t.id, t.norm, t.kind,
                  coalesce(array_agg(DISTINCT s) FILTER (WHERE s IS NOT NULL), '{}') AS sources
           FROM terms t LEFT JOIN doc_terms dt ON dt.term_id = t.id
           LEFT JOIN LATERAL unnest(dt.sources) s ON true
           WHERE t.source = 'auto' AND t.review_en = 'pending' AND t.review_th = 'pending'
           GROUP BY t.id"""
    ).fetchall()
    for r in rows:
        d = default_for(r["norm"], r["kind"], set(r["sources"]))
        conn.execute(
            """UPDATE terms SET decision=%s, review_en=%s, term_th=%s, th_source=%s,
                                updated_at=now() WHERE id=%s""",
            (d["decision"], d["review_en"], d["term_th"], d["th_source"], r["id"]),
        )
    return len(rows)


def export_csv(conn: psycopg.Connection) -> str:
    rows = conn.execute(
        "SELECT term_en, decision, coalesce(term_th, '') AS term_th, kind, "
        "coalesce(notes, '') AS note FROM terms WHERE decision <> 'undecided' ORDER BY norm"
    ).fetchall()
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r[k] for k in CSV_FIELDS})
    return out.getvalue()


def import_csv(conn: psycopg.Connection, text: str) -> dict:
    """Rows from a CSV override the glossary and count as reviewed.

    Columns: term_en (required), decision (keep_en | translate | ignore),
    term_th (required when decision is translate), kind, note.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames or "term_en" not in reader.fieldnames:
        raise ValueError("the CSV needs a 'term_en' column")
    added = updated = skipped = 0
    for row in reader:
        term = (row.get("term_en") or "").strip()
        decision = (row.get("decision") or "").strip() or (
            "translate" if (row.get("term_th") or "").strip() else "keep_en"
        )
        term_th = (row.get("term_th") or "").strip() or None
        if not term or decision not in DECISIONS or (decision == "translate" and not term_th):
            skipped += 1
            continue
        norm = normalize(term)
        kind = (row.get("kind") or "").strip() or ("acronym" if term.isupper() else "term")
        result = conn.execute(
            """INSERT INTO terms (norm, term_en, kind, decision, term_th, source, th_source,
                                  review_en, review_th, notes, reviewed_at)
               VALUES (%s, %s, %s, %s, %s, 'csv', %s, 'approved', %s, %s, now())
               ON CONFLICT (norm) DO UPDATE SET
                 term_en = EXCLUDED.term_en, decision = EXCLUDED.decision,
                 term_th = EXCLUDED.term_th, source = 'csv', th_source = EXCLUDED.th_source,
                 review_en = 'approved', review_th = EXCLUDED.review_th,
                 notes = coalesce(EXCLUDED.notes, terms.notes), updated_at = now(),
                 reviewed_at = now()
               RETURNING (xmax = 0) AS inserted""",
            (
                norm,
                term,
                kind,
                decision,
                term_th,
                "csv" if term_th else None,
                "approved" if decision == "translate" else "pending",
                (row.get("note") or "").strip() or None,
            ),
        ).fetchone()
        if result["inserted"]:
            added += 1
        else:
            updated += 1
    return {"added": added, "updated": updated, "skipped": skipped}
