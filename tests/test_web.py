import re

import pytest
from conftest import chunk_by_text
from fastapi.testclient import TestClient

from pdftr.web.app import create_app

HX = {"HX-Request": "true"}


@pytest.fixture
def client(test_db_url, sample_doc):
    return TestClient(create_app(test_db_url, allowed_hosts=["testserver"]))


def test_pages_render(client):
    assert client.get("/").status_code == 200
    r = client.get("/doc/1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/doc/1/r1"
    html = client.get("/doc/1/r1?page=1").text
    assert 'id="progress"' in html and html.count('class="chunk') == 1
    assert "<code>commands</code>" in html  # markup rendered


def test_later_steps_are_locked(client):
    r = client.get("/doc/1/r2", follow_redirects=False)
    assert r.status_code == 303


def test_actions_need_the_htmx_header(client, conn):
    c = chunk_by_text(conn, 1, "The shell")
    r = client.post(f"/doc/1/chunks/{c['id']}/approve", data={"page": 1, "approved": 1})
    assert r.status_code == 403
    r = client.post(f"/doc/1/chunks/{c['id']}/approve", data={"page": 1, "approved": 1}, headers=HX)
    assert r.status_code == 200 and "✓ approved" in r.text and "hx-swap-oob" in r.text


def test_unknown_hosts_are_refused(test_db_url, sample_doc):
    assert TestClient(create_app(test_db_url)).get("/").status_code == 400


def test_errors_come_back_as_messages(client, conn):
    first = chunk_by_text(conn, 1, "The shell")
    r = client.post(f"/doc/1/chunks/{first['id']}/merge", headers=HX)
    assert r.status_code == 409 and "first chunk" in r.text


def test_complete_r1_then_review_terms(client):
    r = client.post("/doc/1/r1/approve-all", headers=HX)
    assert r.status_code == 204 and "toast-reload" in r.headers["HX-Trigger"]
    r = client.post("/doc/1/r1/complete", headers=HX)
    assert r.headers["HX-Redirect"] == "/doc/1/r2"
    html = client.get("/doc/1/r2?tab=all&status=all").text
    term_id = re.search(r'id="term-(\d+)"', html).group(1)
    r = client.post(f"/doc/1/terms/{term_id}/decision", data={"decision": "keep_en"}, headers=HX)
    assert r.status_code == 200 and f'id="term-{term_id}"' in r.text


def test_glossary_csv_download(client):
    r = client.get("/glossary.csv")
    assert r.status_code == 200 and "ฐานข้อมูล" in r.text


def test_r2_approve_all_terms_then_complete(client, conn):
    client.post("/doc/1/r1/approve-all", headers=HX)
    client.post("/doc/1/r1/complete", headers=HX)
    assert "Approve all" in client.get("/doc/1/r2").text
    r = client.post("/doc/1/r2/complete", headers=HX)
    assert r.status_code == 409  # terms still pending
    r = client.post("/doc/1/terms/approve-all", headers=HX)
    assert r.status_code == 204 and "Approved all" in r.headers["HX-Trigger"]
    pending = conn.execute(
        """SELECT count(*) AS n FROM doc_terms dt JOIN terms t ON t.id = dt.term_id
           WHERE dt.doc_id=1 AND t.review_en='pending'"""
    ).fetchone()["n"]
    assert pending == 0
    r = client.post("/doc/1/r2/complete", headers=HX)
    assert r.headers["HX-Redirect"] == "/doc/1/r3"
