import pytest
from conftest import chunk_by_text

from pdftr import glossary
from pdftr.review import ReviewError, stages
from pdftr.review import chunks as rc
from pdftr.review import terms as rt


def texts(conn, doc_id):
    rows = conn.execute(
        "SELECT text_en FROM chunks WHERE doc_id=%s ORDER BY ord, part", (doc_id,)
    ).fetchall()
    return [r["text_en"] for r in rows]


def test_sample_document_chunks(conn, sample_doc):
    assert texts(conn, sample_doc) == [
        "The shell reads commands from the user and runs them in a subshell.",
        "What runs the kernel?\na. Boot loader\nb. Shell",
        "A database stores data. It has tables. Queries read them.",
    ]
    joined = chunk_by_text(conn, sample_doc, "The shell")
    assert joined["markup_en"] == (
        "The shell reads <c>commands</c> from the user and runs them in a <k>subshell</k>."
    )
    assert joined["page_start"] == 1 and joined["page_end"] == 2


def test_approve_and_progress(conn, sample_doc):
    c = chunk_by_text(conn, sample_doc, "The shell")
    rc.set_approved(conn, c["id"], True)
    prog = rc.progress(conn, sample_doc)
    assert (prog["approved"], prog["pending"], prog["total"]) == (1, 2, 3)
    rc.set_approved(conn, c["id"], False)
    assert rc.progress(conn, sample_doc)["approved"] == 0


def test_edit_keeps_code_and_key_styling(conn, sample_doc):
    c = chunk_by_text(conn, sample_doc, "A database")
    rc.edit_text(conn, c["id"], "A database stores data in tables. Queries read them.")
    row = rc.get_chunk(conn, c["id"])
    assert row["markup_en"] == "A <k>database</k> stores data in tables. Queries read them."
    assert "edited" in row["flags"]
    # single-block chunk: the block (redrawn in Thai later) follows the edit
    block = conn.execute("SELECT text_en FROM blocks WHERE id=%s", (c["block_ids"][0],)).fetchone()
    assert block["text_en"] == "A database stores data in tables. Queries read them."


def test_empty_edit_is_refused(conn, sample_doc):
    c = chunk_by_text(conn, sample_doc, "A database")
    with pytest.raises(ReviewError):
        rc.edit_text(conn, c["id"], "   ")


def test_split_between_blocks_then_merge_back(conn, sample_doc):
    c = chunk_by_text(conn, sample_doc, "The shell")
    new_id = rc.split_chunk(conn, c["id"], 1)
    assert texts(conn, sample_doc)[:2] == [
        "The shell reads commands from the user and",
        "runs them in a subshell.",
    ]
    second = rc.get_chunk(conn, new_id)
    assert second["page_start"] == 2 and second["markup_en"] == "runs them in a <k>subshell</k>."
    rc.merge_with_previous(conn, new_id)
    merged = chunk_by_text(conn, sample_doc, "The shell")
    assert (
        merged["text_en"] == "The shell reads commands from the user and runs them in a subshell."
    )
    assert len(merged["block_ids"]) == 2 and "merged" in merged["flags"]


def test_split_question_keeps_option_markers(conn, sample_doc):
    q = chunk_by_text(conn, sample_doc, "What runs")
    rc.split_chunk(conn, q["id"], 2)  # after option a.
    assert texts(conn, sample_doc)[1:3] == [
        "What runs the kernel?\na. Boot loader",
        "b. Shell",
    ]


def test_split_by_sentence_renumbers_parts(conn, sample_doc):
    c = chunk_by_text(conn, sample_doc, "A database")
    rc.split_chunk(conn, c["id"], 2)
    rc.split_chunk(conn, c["id"], 1)  # split the first half again
    rows = conn.execute(
        "SELECT part, text_en FROM chunks WHERE doc_id=%s AND ord=%s ORDER BY part",
        (sample_doc, c["ord"]),
    ).fetchall()
    assert [(r["part"], r["text_en"]) for r in rows] == [
        (0, "A database stores data."),
        (1, "It has tables."),
        (2, "Queries read them."),
    ]


def test_merge_first_chunk_is_refused(conn, sample_doc):
    first = chunk_by_text(conn, sample_doc, "The shell")
    with pytest.raises(ReviewError):
        rc.merge_with_previous(conn, first["id"])


def test_bulk_approve_page_and_chapter(conn, sample_doc):
    assert rc.approve_page(conn, sample_doc, 1) == 1  # only the joined chunk touches page 1
    title, n = rc.approve_chapter(conn, sample_doc, 2)
    assert title == "Shells" and n == 2
    assert rc.progress(conn, sample_doc)["pending"] == 0


def test_steps_unlock_in_order(conn, sample_doc):
    with pytest.raises(ReviewError, match="3 chunks still pending"):
        stages.complete(conn, sample_doc, "r1")
    with pytest.raises(ReviewError, match="not the current step"):
        stages.complete(conn, sample_doc, "r2")
    rc.approve_all(conn, sample_doc)
    assert stages.complete(conn, sample_doc, "r1") == "r2_terms_en"

    # R2: the seed list drafted the decisions; four terms still need a look
    with pytest.raises(ReviewError, match="4 terms still pending"):
        stages.complete(conn, sample_doc, "r2")
    ids = [r["id"] for r in conn.execute("SELECT id FROM terms").fetchall()]
    rt.approve_en(conn, sample_doc, ids)
    assert stages.complete(conn, sample_doc, "r2") == "r3_terms_th"

    # R3: "database" was drafted with its Thai from the seed list, still to approve
    with pytest.raises(ReviewError, match="1 Thai terms still pending"):
        stages.complete(conn, sample_doc, "r3")
    db = conn.execute("SELECT id, term_th FROM terms WHERE norm='database'").fetchone()
    assert db["term_th"] == "ฐานข้อมูล"
    rt.approve_th(conn, sample_doc, [db["id"]])
    assert stages.complete(conn, sample_doc, "r3") == "translate"


def test_seed_defaults_follow_policy_b(conn, sample_doc):
    rows = {r["norm"]: r for r in conn.execute("SELECT * FROM terms").fetchall()}
    assert rows["database"]["decision"] == "translate"
    assert rows["subshell"]["decision"] == "keep_en"
    assert rows["CPU"]["decision"] == "keep_en"
    assert rows["press enter"]["decision"] == "ignore"
    assert rows["ls"]["decision"] == "keep_en" and rows["ls"]["review_en"] == "approved"


def test_decision_translate_fills_thai_from_seed(conn, sample_doc):
    t = conn.execute("SELECT id FROM terms WHERE norm='subshell'").fetchone()
    rt.set_decision(conn, sample_doc, t["id"], "translate")
    row = rt.get_term(conn, t["id"])
    assert row["decision"] == "translate" and row["term_th"] is None  # not in the seed list
    assert row["review_en"] == "approved"
    rt.set_thai(conn, sample_doc, t["id"], "  ซับเชลล์ ")
    row = rt.get_term(conn, t["id"])
    assert row["term_th"] == "ซับเชลล์" and row["th_source"] == "user"
    assert row["review_th"] == "approved"


def test_add_term_counts_uses_in_the_document(conn, sample_doc):
    term_id = rt.add_term(conn, sample_doc, "shell", "keep_en")
    row = rt.term_row(conn, sample_doc, term_id)
    assert row["freq"] == 2  # "shell" (chunk 1) and "Shell" (option b), not "subshell"
    assert row["first_page"] == 1 and row["source"] == "user"


def test_tabs_split_terms_without_overlap(conn, sample_doc):
    counts = rt.tab_counts(conn, sample_doc)
    groups = sum(counts[t]["total"] for t in ("key", "acronym", "phrase", "code"))
    assert groups == counts["all"]["total"] == 5
    assert counts["key"]["total"] == 2 and counts["code"]["total"] == 1


def test_glossary_csv_round_trip(conn, sample_doc):
    text = glossary.export_csv(conn)
    assert "database,translate,ฐานข้อมูล" in text
    result = glossary.import_csv(
        conn, "term_en,decision,term_th\nsubshell,translate,ซับเชลล์\nnew term,,\nbad,translate,\n"
    )
    assert result == {"added": 1, "updated": 1, "skipped": 1}
    row = conn.execute("SELECT * FROM terms WHERE norm='subshell'").fetchone()
    assert row["term_th"] == "ซับเชลล์" and row["source"] == "csv"
    assert row["review_en"] == "approved" and row["review_th"] == "approved"
