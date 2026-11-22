"""Translation memory: identical work is done once, and your edits are reused.

- machine entries are keyed by engine + prompt version + protected text + rules,
  so a glossary change gives a new key and a fresh translation;
- human entries (R4 edits) are keyed by the source markup alone and always win.
"""

from __future__ import annotations

import hashlib
import json

import psycopg


def key_for(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


def human_key(src_markup: str) -> str:
    return key_for("human", src_markup)


def mt_get(conn: psycopg.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT tgt_text FROM translation_memory WHERE src_hash=%s AND origin='mt' "
        "ORDER BY id DESC LIMIT 1",
        (key,),
    ).fetchone()
    return row["tgt_text"] if row else None


def mt_put(conn: psycopg.Connection, key: str, src_text: str, tgt_text: str, engine: str) -> None:
    conn.execute(
        """INSERT INTO translation_memory (src_hash, src_text, tgt_text, engine, origin)
           VALUES (%s, %s, %s, %s, 'mt')
           ON CONFLICT (src_hash, engine, glossary_rev, origin)
           DO UPDATE SET tgt_text = EXCLUDED.tgt_text, created_at = now()""",
        (key, src_text, tgt_text, engine),
    )


def human_get(conn: psycopg.Connection, src_markup: str) -> str | None:
    row = conn.execute(
        "SELECT tgt_text FROM translation_memory WHERE src_hash=%s AND origin='human' "
        "ORDER BY id DESC LIMIT 1",
        (human_key(src_markup),),
    ).fetchone()
    return row["tgt_text"] if row else None


def human_put(conn: psycopg.Connection, src_markup: str, tgt_markup: str) -> None:
    conn.execute(
        """INSERT INTO translation_memory (src_hash, src_text, tgt_text, engine, origin)
           VALUES (%s, %s, %s, 'human', 'human')
           ON CONFLICT (src_hash, engine, glossary_rev, origin)
           DO UPDATE SET tgt_text = EXCLUDED.tgt_text, created_at = now()""",
        (human_key(src_markup), src_markup, tgt_markup),
    )
