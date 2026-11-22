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
    assert r.headers["HX-Redirect"] == "/"


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
