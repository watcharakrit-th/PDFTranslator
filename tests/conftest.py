"""Shared fixtures. Database tests run in a throwaway database (never the real one)."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from pdftr.ingest import store
from pdftr.ingest.cutter import cut
from pdftr.ingest.models import Block, TermCandidate
from pdftr.settings import get_settings


@pytest.fixture(scope="session")
def test_db_url():
    base = get_settings().database_url
    try:
        admin = psycopg.connect(base, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("database not running (start it with: pdftr db up)")
    name = f"pdftr_test_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    yield base.rsplit("/", 1)[0] + "/" + name
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


@pytest.fixture
def conn(test_db_url):
    from pdftr.db import connect, init_schema

    c = connect(test_db_url)
    init_schema(c)
    c.execute(
        "TRUNCATE documents, terms, translation_memory, jobs, review_log RESTART IDENTITY CASCADE"
    )
    c.commit()
    yield c
    c.close()


def _block(page, seq, role, markup, marker=None, level=None):
    from pdftr.ingest.textutil import strip_markup

    return Block(
        page_no=page,
        seq=seq,
        role=role,
        docling_label=role,
        bbox=(50, 50 + 20 * seq, 500, 65 + 20 * seq),
        markup=markup,
        text=strip_markup(markup),
        marker=marker,
        level=level,
    )


@pytest.fixture
def sample_doc(conn):
    """A two-page document: a paragraph that runs across the page break, a
    multiple-choice question, and a three-sentence paragraph. Returns doc id."""
    blocks = [
        _block(1, 1, "heading", "Shells", level=1),
        _block(1, 2, "paragraph", "The shell reads <c>commands</c> from the user and"),
        _block(2, 1, "paragraph", "runs them in a <k>subshell</k>."),
        _block(2, 2, "list_item", "What runs the kernel?", marker="1.", level=0),
        _block(2, 3, "list_item", "Boot loader", marker="a.", level=1),
        _block(2, 4, "list_item", "Shell", marker="b.", level=1),
        _block(
            2, 5, "paragraph", "A <k>database</k> stores data. It has tables. Queries read them."
        ),
    ]
    kinds = {1: "body", 2: "body"}
    chunks = cut(blocks, kinds, vocab=set())
    doc_id = store.create_document(
        conn,
        file_hash="x" * 64,
        file_name="sample.pdf",
        file_path="sample.pdf",
        title="Sample",
        page_count=2,
    )
    store.save_pages(
        conn,
        doc_id,
        {
            1: {"label": "1", "kind": "body", "width": 612, "height": 792},
            2: {"label": "2", "kind": "body", "width": 612, "height": 792},
        },
    )
    store.save_blocks(conn, doc_id, blocks)
    store.save_chunks(conn, doc_id, chunks)
    store.save_terms(
        conn,
        doc_id,
        [
            TermCandidate(
                norm="database",
                term="database",
                kind="term",
                sources={"styled"},
                freq=1,
                first_page=2,
                example="A database stores data.",
            ),
            TermCandidate(
                norm="subshell",
                term="subshell",
                kind="term",
                sources={"styled"},
                freq=1,
                first_page=2,
                example="runs them in a subshell.",
            ),
            TermCandidate(
                norm="CPU", term="CPU", kind="acronym", sources={"acronym"}, freq=3, first_page=1
            ),
            TermCandidate(
                norm="press enter",
                term="press enter",
                kind="term",
                sources={"ngram"},
                freq=5,
                first_page=1,
            ),
            TermCandidate(
                norm="ls", term="ls", kind="code", sources={"code"}, freq=4, first_page=1
            ),
        ],
    )
    conn.commit()
    return doc_id


def chunk_by_text(conn, doc_id, start):
    return conn.execute(
        "SELECT * FROM chunks WHERE doc_id=%s AND text_en LIKE %s ORDER BY ord, part",
        (doc_id, start + "%"),
    ).fetchone()
