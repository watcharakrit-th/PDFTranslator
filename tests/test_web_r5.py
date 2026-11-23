import pytest
from conftest import FakeEmbedder
from fastapi.testclient import TestClient

from pdftr.translate.runner import translate_document
from pdftr.web.app import create_app

HX = {"HX-Request": "true"}


@pytest.fixture
def client(test_db_url, fake_engine):
    app = create_app(
        test_db_url, allowed_hosts=["testserver"], engine=fake_engine, embedder=FakeEmbedder()
    )
    return TestClient(app)


@pytest.fixture
def at_r5(conn, sample_doc, fake_engine):
    translate_document(conn, sample_doc, fake_engine, log=lambda _m: None)
    conn.execute(
        "UPDATE chunks SET review_en='approved', review_th='approved' WHERE doc_id=%s",
        (sample_doc,),
    )
    conn.execute("UPDATE documents SET stage='r5_embed' WHERE id=%s", (sample_doc,))
    conn.commit()
    return sample_doc


def test_r5_is_locked_before_r4(client, sample_doc):
    assert client.get("/doc/1/r5", follow_redirects=False).status_code == 303


def test_r5_shows_what_is_embedded_and_approves(client, conn, at_r5):
    r = client.get("/doc/1/r5?page=2")
    assert r.status_code == 200 and "Approve embedding" in r.text
    assert "หัวข้อ:" in r.text and "Section: Shells" in r.text  # the exact embedded texts
    r = client.post("/doc/1/r5/complete", headers=HX)
    assert r.status_code == 204
    doc = conn.execute("SELECT stage, embed_approved_at FROM documents WHERE id=1").fetchone()
    assert doc["stage"] == "done" and doc["embed_approved_at"] is not None
    assert "pdftr embed 1" in client.get("/doc/1/r5").text


def test_search_page(client, conn, at_r5):
    from pdftr.embed.runner import embed_document

    assert "Nothing found" in client.get("/search?q=database").text
    embed_document(conn, at_r5, FakeEmbedder(), log=lambda _m: None)
    r = client.get("/search?q=A database stores data")
    assert r.status_code == 200 and "A database stores data" in r.text and "p. 2" in r.text
