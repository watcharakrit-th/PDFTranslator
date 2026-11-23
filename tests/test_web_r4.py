import pytest
from conftest import chunk_by_text
from fastapi.testclient import TestClient

from pdftr.translate.runner import translate_document
from pdftr.web.app import create_app

HX = {"HX-Request": "true"}


@pytest.fixture
def translated(conn, sample_doc, fake_engine):
    conn.execute("UPDATE documents SET stage='translate' WHERE id=%s", (sample_doc,))
    conn.commit()
    translate_document(conn, sample_doc, fake_engine, log=lambda _m: None)
    conn.commit()
    return sample_doc


@pytest.fixture
def client(test_db_url, fake_engine):
    return TestClient(create_app(test_db_url, allowed_hosts=["testserver"], engine=fake_engine))


def test_r4_is_locked_before_translation(client, sample_doc):
    assert client.get("/doc/1/r4", follow_redirects=False).status_code == 303


def test_r4_page_with_thai_preview(client, translated):
    r = client.get("/doc/1/r4?page=2")
    assert r.status_code == 200 and "ฐานข้อมูล" in r.text
    png = client.get("/doc/1/page/2/thai.png")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"


def test_edit_approve_retranslate_and_complete(client, conn, translated):
    db = chunk_by_text(conn, translated, "A database")
    r = client.post(
        f"/doc/1/chunks/{db['id']}/th/edit", data={"text": "ฐานข้อมูลเก็บข้อมูล"}, headers=HX
    )
    assert r.status_code == 204 and r.headers["HX-Refresh"] == "true"
    joined = chunk_by_text(conn, translated, "The shell")
    r = client.post(
        f"/doc/1/chunks/{joined['id']}/th/approve", data={"page": 1, "approved": 1}, headers=HX
    )
    assert r.status_code == 200 and "✓ approved" in r.text
    r = client.post(f"/doc/1/chunks/{joined['id']}/th/retranslate", headers=HX)
    assert r.status_code == 204
    r = client.post("/doc/1/r4/complete", headers=HX)
    assert r.status_code == 409  # still pending chunks
    client.post("/doc/1/r4/approve-all", headers=HX)
    r = client.post("/doc/1/r4/complete", headers=HX)
    assert r.headers["HX-Redirect"] == "/doc/1/r5"  # R5 is next


def test_glossary_flag_names_the_term(client, conn, translated):
    db = chunk_by_text(conn, translated, "A database")
    conn.execute(
        "UPDATE chunks SET text_th='คลังข้อมูลเก็บข้อมูล', markup_th='คลังข้อมูลเก็บข้อมูล', "
        "tr_flags='{glossary}' WHERE id=%s",
        (db["id"],),
    )
    conn.commit()
    r = client.get("/doc/1/r4?page=2")
    assert "Glossary: database → ฐานข้อมูล" in r.text


def test_other_text_heading_edit_flows_to_its_bookmark(client, conn, translated):
    r = client.get("/doc/1/r4?page=1")
    assert "Other text on this page" in r.text and "Bookmark" in r.text
    heading = conn.execute(
        "SELECT id FROM blocks WHERE doc_id=%s AND role='heading'", (translated,)
    ).fetchone()["id"]
    r = client.post(f"/doc/1/blocks/{heading}/th/edit", data={"text": "เชลล์"}, headers=HX)
    assert r.status_code == 204
    entry = conn.execute("SELECT * FROM outline_entries WHERE title_en='Shells'").fetchone()
    assert entry["title_th"] == "เชลล์" and entry["tr_source"] == "heading"
    sub = conn.execute("SELECT id FROM outline_entries WHERE title_en='Subshells'").fetchone()
    r = client.post(f"/doc/1/outline/{sub['id']}/th/edit", data={"text": "ซับเชลล์"}, headers=HX)
    assert r.status_code == 204
    assert "ซับเชลล์" in client.get("/doc/1/r4?page=2").text
    r = client.post(f"/doc/2/blocks/{heading}/th/edit", data={"text": "x"}, headers=HX)
    assert r.status_code == 409  # another document's block


def test_other_text_cell_edit(client, conn, translated):
    from pdftr.ingest import store
    from pdftr.ingest.models import Block

    store.save_blocks(
        conn, translated, [Block(2, 9, "toc", "document_index", (40, 295, 420, 340), "", "")]
    )
    conn.execute("UPDATE blocks SET cells_th='{\"t:working with files\": \"x\"}' WHERE role='toc'")
    conn.commit()
    toc = conn.execute("SELECT id FROM blocks WHERE role='toc'").fetchone()["id"]
    assert "Working with files" in client.get("/doc/1/r4?page=2").text  # listed for editing
    r = client.post(
        f"/doc/1/blocks/{toc}/cells/th/edit",
        data={"pos": "t:working with files", "text": "การทำงานกับไฟล์"}, headers=HX,
    )  # fmt: skip
    assert r.status_code == 204
    row = conn.execute("SELECT cells_th FROM blocks WHERE id=%s", (toc,)).fetchone()
    assert row["cells_th"]["t:working with files"] == "การทำงานกับไฟล์"
