import threading

import pytest
from conftest import FakeEngine, chunk_by_text
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


# ------------------------------------------------------------------ Translate all
class GateEngine(FakeEngine):
    """FakeEngine whose first request waits until the test opens the gate."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.gate = threading.Event()

    def translate(self, text, rules, *, retry=False, stop=None):
        if not self.entered.is_set():
            self.entered.set()
            assert self.gate.wait(timeout=20)
        return super().translate(text, rules, retry=retry, stop=stop)


def _ready_to_translate(conn, doc_id):
    conn.execute("UPDATE documents SET stage='translate' WHERE id=%s", (doc_id,))
    conn.commit()


def _missing(conn, doc_id):
    sql = "SELECT count(*) AS n FROM chunks WHERE doc_id=%s AND text_th IS NULL"
    return conn.execute(sql, (doc_id,)).fetchone()["n"]


def test_translate_all_needs_the_glossary_first(client, sample_doc):
    r = client.post("/doc/1/translate/start", headers=HX)
    assert r.status_code == 409 and "R1–R3" in r.text


def test_translate_all_runs_the_whole_book(client, conn, sample_doc):
    _ready_to_translate(conn, sample_doc)
    page = client.get("/doc/1/r4?page=2").text
    assert 'id="translate-all"' in page and "chunks have no Thai yet" in page
    r = client.post("/doc/1/translate/start", headers=HX)
    assert r.status_code == 204 and r.headers["HX-Refresh"] == "true"
    job = client.app.state.job
    job.join(timeout=30)
    assert job.state == "finished", job.error
    stage = conn.execute("SELECT stage FROM documents WHERE id=%s", (sample_doc,)).fetchone()
    assert stage["stage"] == "r4_chunks_th" and _missing(conn, sample_doc) == 0
    r = client.get("/doc/1/translate/status", headers=HX)  # the page's next poll
    assert r.status_code == 204 and r.headers["HX-Refresh"] == "true"
    page = client.get("/doc/1/r4?page=2").text
    assert 'id="translate-all"' not in page and "ฐานข้อมูล" in page


def test_translate_all_while_reviewing(test_db_url, conn, sample_doc):
    """The page keeps working during a run: progress, one run at a time, no per-chunk
    re-translation, and Thai you write meanwhile is not overwritten."""
    engine = GateEngine()
    client = TestClient(create_app(test_db_url, allowed_hosts=["testserver"], engine=engine))
    _ready_to_translate(conn, sample_doc)
    db = chunk_by_text(conn, sample_doc, "A database")
    try:
        assert client.post("/doc/1/translate/start", headers=HX).status_code == 204
        assert engine.entered.wait(timeout=20)  # the first request is in progress
        r = client.get("/doc/1/translate/status", headers=HX)
        assert r.status_code == 200 and 'hx-trigger="every 3s"' in r.text and "Stop" in r.text
        assert 'hx-trigger="every 3s"' in client.get("/doc/1/r4?page=2").text
        r = client.post("/doc/1/translate/start", headers=HX)
        assert r.status_code == 409 and "running" in r.text
        r = client.post(f"/doc/1/chunks/{db['id']}/th/retranslate", headers=HX)
        assert r.status_code == 409 and "running" in r.text
        r = client.post(
            f"/doc/1/chunks/{db['id']}/th/edit", data={"text": "ฐานข้อมูลของฉัน"}, headers=HX
        )
        assert r.status_code == 204
    finally:
        engine.gate.set()
    job = client.app.state.job
    job.join(timeout=30)
    assert job.state == "finished", job.error
    row = chunk_by_text(conn, sample_doc, "A database")
    assert row["text_th"] == "ฐานข้อมูลของฉัน" and row["tr_source"] == "human"
    assert _missing(conn, sample_doc) == 0


def test_translate_all_stop_and_continue(test_db_url, conn, sample_doc):
    engine = GateEngine()
    client = TestClient(create_app(test_db_url, allowed_hosts=["testserver"], engine=engine))
    _ready_to_translate(conn, sample_doc)
    try:
        assert client.post("/doc/1/translate/start", headers=HX).status_code == 204
        assert engine.entered.wait(timeout=20)
        r = client.post("/doc/1/translate/stop", headers=HX)
        assert r.status_code == 200 and "Stopping" in r.text
    finally:
        engine.gate.set()
    job = client.app.state.job
    job.join(timeout=30)
    assert job.state == "stopped" and job.summary["stopped"]
    missing = _missing(conn, sample_doc)
    total = conn.execute("SELECT count(*) AS n FROM chunks WHERE doc_id=%s", (sample_doc,))
    assert 0 < missing < total.fetchone()["n"]  # the piece in progress was finished and kept
    stage = conn.execute("SELECT stage FROM documents WHERE id=%s", (sample_doc,)).fetchone()
    assert stage["stage"] == "translate"  # not finished yet
    page = client.get("/doc/1/r4?page=2").text
    assert "Stopped." in page and 'id="translate-all"' in page
    assert client.post("/doc/1/translate/start", headers=HX).status_code == 204  # continue
    job = client.app.state.job
    job.join(timeout=30)
    assert job.state == "finished" and _missing(conn, sample_doc) == 0
