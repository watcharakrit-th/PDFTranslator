"""Embedding (M5): Thai contexts, what gets embedded, re-runs, and search."""

import pytest
from conftest import chunk_by_text

from pdftr.embed.context import _intro_thai, thai_contexts
from pdftr.embed.runner import embed_document, plan
from pdftr.embed.search import search
from pdftr.gpu import GpuGuard
from pdftr.translate.runner import translate_document

QUIET = {"log": lambda _m: None}


@pytest.fixture
def reviewed(conn, sample_doc, fake_engine):
    """Translated, and approved in R1 and R4."""
    translate_document(conn, sample_doc, fake_engine, **QUIET)
    conn.execute(
        "UPDATE chunks SET review_en='approved', review_th='approved' WHERE doc_id=%s",
        (sample_doc,),
    )
    conn.commit()
    return sample_doc


def test_thai_context_uses_the_heading_thai(conn, reviewed):
    heading = conn.execute(
        "SELECT markup_th FROM blocks WHERE doc_id=%s AND role='heading'", (reviewed,)
    ).fetchone()["markup_th"]
    db = chunk_by_text(conn, reviewed, "A database")
    assert db["context_en"].startswith("Section: Shells")
    assert thai_contexts(conn, reviewed)[db["id"]] == f"หัวข้อ: {heading}"


def test_intro_is_the_matching_end_of_the_thai_paragraph():
    chunks = [
        {"text_en": "Linux has many parts. The kernel versions have three numbers:",
         "text_th": "ลินุกซ์มีหลายส่วน เวอร์ชันของเคอร์เนลมีตัวเลขสามตัว:"},
        {"text_en": "Major number", "text_th": "หมายเลขหลัก"},
    ]  # fmt: skip
    intro = _intro_thai("The kernel versions have three numbers:", chunks, 1)
    assert intro == "เวอร์ชันของเคอร์เนลมีตัวเลขสามตัว:"


def test_embeds_english_and_thai_then_only_changes(conn, reviewed, fake_embedder):
    first = embed_document(conn, reviewed, fake_embedder, **QUIET)
    rows = conn.execute(
        """SELECT kind, embedding_en IS NOT NULL AS en, embedding_th IS NOT NULL AS th
           FROM chunks WHERE doc_id=%s""",
        (reviewed,),
    ).fetchall()
    assert all(r["en"] and r["th"] for r in rows)
    assert first["embedded_now"] == 2 * len(rows) and first["up_to_date"] == 0
    again = embed_document(conn, reviewed, fake_embedder, **QUIET)
    assert again["embedded_now"] == 0 and again["up_to_date"] == 2 * len(rows)
    db = chunk_by_text(conn, reviewed, "A database")
    conn.execute("UPDATE chunks SET text_th='ฐานข้อมูลเก็บข้อมูล' WHERE id=%s", (db["id"],))
    conn.commit()
    assert embed_document(conn, reviewed, fake_embedder, **QUIET)["embedded_now"] == 1


def test_kept_out_and_unapproved_chunks_are_not_embedded(conn, reviewed, fake_embedder):
    embed_document(conn, reviewed, fake_embedder, **QUIET)
    db = chunk_by_text(conn, reviewed, "A database")
    shell = chunk_by_text(conn, reviewed, "The shell")
    conn.execute("UPDATE chunks SET embed=false WHERE id=%s", (db["id"],))
    conn.execute("UPDATE chunks SET review_th='pending' WHERE id=%s", (shell["id"],))
    conn.commit()
    result = embed_document(conn, reviewed, fake_embedder, **QUIET)
    assert result["cleared"] == 3  # the kept-out chunk (both languages) and one Thai
    states = {p.id: p for p in plan(conn, reviewed, "fake")}
    assert states[db["id"]].en_reason == "kept out (R1, or a Key Terms list)"
    assert states[shell["id"]].en == "embedded" and states[shell["id"]].th_reason == (
        "Thai not approved in R4"
    )


def test_search_finds_chunks_in_either_language(conn, reviewed, fake_embedder):
    assert search(conn, fake_embedder, "database") == []  # nothing embedded yet
    embed_document(conn, reviewed, fake_embedder, **QUIET)
    english = search(conn, fake_embedder, "A database stores data. It has tables.")
    assert english[0].text_en.startswith("A database") and english[0].lang == "en"
    thai_text = chunk_by_text(conn, reviewed, "A database")["text_th"]
    thai = search(conn, fake_embedder, thai_text, langs=("th",))
    assert thai[0].text_en.startswith("A database") and thai[0].lang == "th"
    assert thai[0].label == "2" and thai[0].section_path == ["Shells"]


def test_power_limit_check(monkeypatch):
    guard = GpuGuard(log=lambda _m: None)
    monkeypatch.setattr(GpuGuard, "power_limit_w", lambda self: 235.0)
    assert "nvidia-smi -pl 150" in guard.power_problem(150)
    assert guard.power_problem(0) is None  # not checked
    monkeypatch.setattr(GpuGuard, "power_limit_w", lambda self: 150.0)
    assert guard.power_problem(150) is None
