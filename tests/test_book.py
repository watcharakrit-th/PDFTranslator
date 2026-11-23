"""The bilingual PDF: layouts, copied pages, bookmarks, labels, contents pages."""

import pymupdf
import pytest

from pdftr.ingest import store
from pdftr.ingest.models import Block
from pdftr.render.book import _label_rules, render_book
from pdftr.render.thai_page import render_thai_page
from pdftr.translate.runner import translate_document

QUIET = {"log": lambda _m: None}


@pytest.fixture
def translated(conn, sample_doc, fake_engine):
    translate_document(conn, sample_doc, fake_engine, **QUIET)
    conn.commit()
    return sample_doc


def _halves(sheet):
    w = sheet.rect.width / 2
    left = sheet.get_text(clip=pymupdf.Rect(0, 0, w, sheet.rect.height))
    right = sheet.get_text(clip=pymupdf.Rect(w, 0, 2 * w, sheet.rect.height))
    return left, right


def test_side_by_side_pairs_each_page_with_its_thai(conn, translated, tmp_path):
    result = render_book(conn, translated, tmp_path, **QUIET)
    out = pymupdf.open(result.path)
    assert out.page_count == 2 and result.thai_pages == 2 and result.overflow == 0
    assert out[1].rect.width == 2 * 612
    left, right = _halves(out[1])
    assert "Sample page 2" in left and "ฐานข้อมูล" not in left
    assert "ฐานข้อมูล" in right  # the Thai page, with the glossary's Thai term
    assert out[1].get_label() == "2"


def test_bookmarks_use_the_heading_thai(conn, translated, tmp_path):
    out = pymupdf.open(render_book(conn, translated, tmp_path, **QUIET).path)
    heading = conn.execute(
        "SELECT markup_th FROM blocks WHERE doc_id=%s AND role='heading'", (translated,)
    ).fetchone()["markup_th"]
    entries = conn.execute(
        "SELECT title_en, title_th, tr_source FROM outline_entries ORDER BY seq"
    ).fetchall()
    assert entries[0]["tr_source"] == "heading" and entries[0]["title_th"] == heading
    assert entries[1]["title_th"] == "Subshells"  # a Keep English term stays English
    assert out.get_toc() == [[1, heading, 1], [2, "Subshells", 2]]


def test_alternate_layout_and_copied_pages(conn, translated, tmp_path):
    conn.execute("UPDATE pages SET kind='cover' WHERE doc_id=%s AND page_no=1", (translated,))
    conn.commit()
    result = render_book(conn, translated, tmp_path, layout="alternate", **QUIET)
    out = pymupdf.open(result.path)
    assert out.page_count == 4 and result.thai_pages == 1
    assert out[1].get_text() == out[0].get_text()  # the cover stays English
    assert "ฐานข้อมูล" in out[3].get_text()
    assert [out[i].get_label() for i in range(4)] == ["1", "1 TH", "2", "2 TH"]


def test_label_rules_compress_numbered_runs():
    rules = _label_rules(["i", "ii", "1", "2", "3", "A-1"])
    assert rules == [
        {"startpage": 0, "prefix": "i", "style": "", "firstpagenum": 1},
        {"startpage": 1, "prefix": "ii", "style": "", "firstpagenum": 1},
        {"startpage": 2, "prefix": "", "style": "D", "firstpagenum": 1},
        {"startpage": 5, "prefix": "A-1", "style": "", "firstpagenum": 1},
    ]


CONTENTS_RECT = (40, 295, 420, 340)  # the two contents lines on page 2 of the sample PDF


def test_contents_titles_reuse_heading_thai(conn, sample_doc, fake_engine):
    toc = Block(2, 9, "toc", "document_index", CONTENTS_RECT, "", "", style={"cells": []})
    store.save_blocks(conn, sample_doc, [toc])
    conn.commit()
    translate_document(conn, sample_doc, fake_engine, **QUIET)
    conn.commit()
    row = conn.execute(
        "SELECT cells_th FROM blocks WHERE doc_id=%s AND role='toc'", (sample_doc,)
    ).fetchone()
    heading = conn.execute(
        "SELECT markup_th FROM blocks WHERE doc_id=%s AND role='heading'", (sample_doc,)
    ).fetchone()["markup_th"]
    assert row["cells_th"]["t:shells"] == heading  # same Thai as the chapter heading
    assert row["cells_th"]["t:working with files"]  # other titles are translated
    assert len(row["cells_th"]) == 2  # page numbers stay as they are


def test_a_table_on_a_contents_page_is_contents(conn, sample_doc, fake_engine):
    conn.execute("UPDATE pages SET kind='toc' WHERE doc_id=%s AND page_no=2", (sample_doc,))
    table = Block(2, 9, "table", "table", CONTENTS_RECT, "", "", style={"cells": []})
    store.save_blocks(conn, sample_doc, [table])
    conn.commit()
    translate_document(conn, sample_doc, fake_engine, **QUIET)
    conn.commit()
    row = conn.execute(
        "SELECT cells_th FROM blocks WHERE doc_id=%s AND role='table'", (sample_doc,)
    ).fetchone()
    heading = conn.execute(
        "SELECT markup_th FROM blocks WHERE doc_id=%s AND role='heading'", (sample_doc,)
    ).fetchone()["markup_th"]
    assert row["cells_th"]["t:shells"] == heading


def test_contents_page_is_drawn_with_page_numbers_kept(tmp_path):
    pdf = pymupdf.open()
    page = pdf.new_page(width=612, height=792)
    page.insert_text((50, 310), "Shells", fontsize=10)
    page.insert_text((400, 310), "15", fontsize=10)
    path = tmp_path / "toc.pdf"
    pdf.save(path)
    spans = [
        s for b in pymupdf.open(path)[0].get_text("dict")["blocks"] for ln in b["lines"]
        for s in ln["spans"]
    ]  # fmt: skip
    title, number = (next(s["bbox"] for s in spans if s["text"] == t) for t in ("Shells", "15"))
    block = {
        "id": 1, "role": "toc", "x0": 50, "y0": 290, "x1": 420, "y1": 320,
        "style": {"cells": [
            {"row": 0, "col": 0, "bbox": list(title), "text": "Shells"},
            {"row": 0, "col": 1, "bbox": list(number), "text": "15"},
        ]},
        "translate": True, "markup_th": None, "cells_th": {"t:shells": "การใช้งานเชลล์ขั้นพื้นฐาน"},
    }  # fmt: skip
    doc, fits = render_thai_page(path, 1, [block])
    text = doc[0].get_text()
    assert "การใช้งานเชลล์ขั้นพื้นฐาน" in text and "15" in text and "Shells" not in text
    assert fits[0].scale == 1.0  # the title widened instead of shrinking
