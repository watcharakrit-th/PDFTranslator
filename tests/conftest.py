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
def sample_pdf(tmp_path):
    """A real two-page PDF for the sample document (the Thai preview needs one)."""
    import pymupdf

    pdf = pymupdf.open()
    for page_no in (1, 2):
        page = pdf.new_page(width=612, height=792)
        page.insert_text((50, 60), f"Sample page {page_no}", fontsize=10)
    contents = pdf[1]  # two contents lines, for tests that add a contents block (40, 295, 420, 340)
    for y, title, number in ((310, "Shells", "1"), (330, "Working with files", "2")):
        contents.insert_text((50, y), title, fontsize=10)
        contents.insert_text((400, y), number, fontsize=10)
    pdf.set_toc([[1, "Shells", 1], [2, "Subshells", 2]])
    path = tmp_path / "sample.pdf"
    pdf.save(path)
    return str(path)


class FakeEngine:
    """Deterministic stand-in for the translator: follows the glossary rules, keeps
    placeholders, and turns every other English word into Thai letters."""

    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def translate(self, text, rules, *, retry=False, stop=None):
        import re

        from pdftr.translate.engine import Result

        self.calls += 1
        joined = "\n".join(rules)
        stash: dict[str, str] = {}

        def keep(value: str) -> str:
            key = f"@{len(stash)}@"
            stash[key] = value
            return key

        out = text
        for en, th in re.findall(r'Translate "(.+?)" as "(.+?)"', joined):
            out = re.sub(
                re.escape(en) + r"s?", lambda _m, th=th: keep(th), out, flags=re.IGNORECASE
            )
        for en in re.findall(r'Keep "(.+?)" in English', joined):
            out = re.sub(
                re.escape(en) + r"s?", lambda m: keep(m.group(0)), out, flags=re.IGNORECASE
            )
        out = re.sub(r"[A-Za-z]+", lambda m: "ก" * len(m.group(0)), out)
        for key, value in stash.items():
            out = out.replace(key, value)
        return Result(out, 0.01, 10)


@pytest.fixture
def fake_engine():
    return FakeEngine()


class FakeEmbedder:
    """Deterministic stand-in for BGE-M3: character trigrams hashed into 1024 dimensions,
    so similar texts get similar unit vectors."""

    name = "fake"

    def __init__(self) -> None:
        self.texts = 0

    def encode(self, texts):
        import zlib

        import numpy as np

        self.texts += len(texts)
        out = np.zeros((len(texts), 1024), dtype=np.float32)
        for i, text in enumerate(texts):
            t = " ".join(text.lower().split())
            for j in range(max(1, len(t) - 2)):
                out[i, zlib.crc32(t[j : j + 3].encode()) % 1024] += 1.0
            out[i] /= np.linalg.norm(out[i]) or 1.0
        return out


@pytest.fixture
def fake_embedder():
    return FakeEmbedder()


@pytest.fixture
def sample_doc(conn, sample_pdf):
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
        file_path=sample_pdf,
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
