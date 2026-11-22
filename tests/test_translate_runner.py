import pytest
from conftest import FakeEngine, chunk_by_text

from pdftr.gpu import GpuTooHot
from pdftr.review import thai as rth
from pdftr.translate.rules import load_glossary
from pdftr.translate.runner import Glossary, translate_document, translate_text


def run(conn, doc_id, engine, **kw):
    summary = translate_document(conn, doc_id, engine, log=lambda _m: None, **kw)
    conn.commit()
    return summary


def test_translates_chunks_and_fills_page_blocks(conn, sample_doc, fake_engine):
    run(conn, sample_doc, fake_engine)
    joined = chunk_by_text(conn, sample_doc, "The shell")
    assert "<c>commands</c>" in joined["markup_th"]  # inline code survives
    assert joined["tr_source"] == "mt" and joined["review_th"] == "pending"
    blocks = conn.execute(
        "SELECT markup_th, tr_flags FROM blocks WHERE id = ANY(%s) ORDER BY ord",
        (joined["block_ids"],),
    ).fetchall()
    assert all(b["markup_th"] for b in blocks)  # the paragraph is split over both pages
    assert "split_th" in blocks[1]["tr_flags"]
    db = chunk_by_text(conn, sample_doc, "A database")
    assert "ฐานข้อมูล" in db["text_th"]  # glossary rule from the seed list
    mcq = chunk_by_text(conn, sample_doc, "What runs")
    assert mcq["text_th"].count("\n") == 2 and "\na. " in mcq["text_th"]


def test_second_run_reuses_everything(conn, sample_doc, fake_engine):
    run(conn, sample_doc, fake_engine)
    calls = fake_engine.calls
    summary = run(conn, sample_doc, fake_engine)
    assert fake_engine.calls == calls and summary.get("mt", 0) == 0


def test_glossary_change_retranslates_only_affected_text(conn, sample_doc, fake_engine):
    run(conn, sample_doc, fake_engine)
    calls = fake_engine.calls
    conn.execute("UPDATE terms SET term_th='คลังข้อมูล' WHERE norm='database'")
    conn.commit()
    summary = run(conn, sample_doc, fake_engine)
    assert summary["mt"] == 1 and fake_engine.calls == calls + 1
    assert "คลังข้อมูล" in chunk_by_text(conn, sample_doc, "A database")["text_th"]


def test_your_edit_wins_over_the_machine(conn, sample_doc, fake_engine):
    run(conn, sample_doc, fake_engine)
    db = chunk_by_text(conn, sample_doc, "A database")
    rth.edit_thai(conn, db["id"], "ฐานข้อมูลเก็บข้อมูลไว้ในตาราง")
    conn.commit()
    run(conn, sample_doc, fake_engine, force=True)
    again = chunk_by_text(conn, sample_doc, "A database")
    assert again["text_th"] == "ฐานข้อมูลเก็บข้อมูลไว้ในตาราง" and again["tr_source"] == "human"


def test_translation_unlocks_r4(conn, sample_doc, fake_engine):
    conn.execute("UPDATE documents SET stage='translate' WHERE id=%s", (sample_doc,))
    conn.commit()
    summary = run(conn, sample_doc, fake_engine)
    assert summary["stage_advanced"] is True
    stage = conn.execute("SELECT stage FROM documents WHERE id=%s", (sample_doc,)).fetchone()
    assert stage["stage"] == "r4_chunks_th"


# ------------------------------------------------------------------ GPU heat stops
class HotEngine(FakeEngine):
    """The GPU guard stops every request longer than ``limit`` words."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.stopped = 0

    def translate(self, text, rules, *, retry=False, stop=None):
        if len(text.split()) > self.limit:
            self.stopped += 1
            raise GpuTooHot(91, 90)
        return super().translate(text, rules, retry=retry)


def test_too_hot_text_is_retried_in_shorter_pieces(conn, sample_doc):
    numbers = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
    text = " ".join(
        f"The kernel schedules process number {n} on the processor now." for n in numbers
    )
    engine = HotEngine(limit=40)  # 10 words per sentence, 90 in all
    glossary = Glossary(load_glossary(conn, sample_doc))
    thai, _key, outcome = translate_text(conn, engine, glossary, text)
    assert engine.stopped == 1 and engine.calls == 3  # 90 words -> 40 + 40 + 10
    assert outcome.source == "mt" and thai.count(".") == 9


def test_short_text_that_stays_too_hot_stops_the_run(conn, sample_doc):
    engine = HotEngine(limit=0)
    with pytest.raises(GpuTooHot):
        translate_text(conn, engine, Glossary(load_glossary(conn, sample_doc)), "Short text.")
    assert engine.stopped == 2  # one more try after cooling down, then stop


def test_a_stopped_run_keeps_its_work_and_resumes(conn, sample_doc, fake_engine):
    class StopsLater(FakeEngine):
        def translate(self, text, rules, *, retry=False, stop=None):
            if self.calls >= 2:
                raise GpuTooHot(91, 90)
            return super().translate(text, rules, retry=retry)

    with pytest.raises(GpuTooHot):
        translate_document(conn, sample_doc, StopsLater(), log=lambda _m: None)
    conn.rollback()
    saved = "SELECT count(*) AS n FROM chunks WHERE doc_id=%s AND tr_source='mt'"
    assert conn.execute(saved, (sample_doc,)).fetchone()["n"] == 2
    summary = run(conn, sample_doc, fake_engine)
    assert summary["unchanged"] == 2 and summary["mt"] > 0


def test_text_of_only_kept_terms_skips_the_model(conn, sample_doc, fake_engine):
    glossary = Glossary(load_glossary(conn, sample_doc))
    assert any(t.term_en == "CPU" and t.decision == "keep_en" for t in glossary.terms)
    thai, _key, outcome = translate_text(conn, fake_engine, glossary, "CPU/CPUs (2)")
    assert thai == "CPU/CPUs (2)" and outcome.source == "kept" and fake_engine.calls == 0
    _thai, _key, outcome = translate_text(conn, fake_engine, glossary, "The CPU")
    assert outcome.source == "mt" and fake_engine.calls == 1
