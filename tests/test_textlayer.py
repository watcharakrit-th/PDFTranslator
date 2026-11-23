"""The Thai text layer: what a reader gets when copying or searching the Thai page."""

import re

import pymupdf

from pdftr.render.textlayer import _drop_spaces, _unicode_of, face_info, fix_text_layer
from pdftr.render.thai_page import BASE_CSS, FONT_DIR, render_thai_page, to_html

SOURCE = (
    "คำสั่ง <c>ls -la</c> แสดงไฟล์ทั้งหมดที่อยู่ในไดเรกทอรี รวมถึงไฟล์ที่ซ่อนอยู่ "
    "<b>หมายเหตุ</b> น้ำหนักของระบบปฏิบัติการ Linux ต่ำกว่าระบบอื่นซ้ำแล้วซ้ำเล่า"
)


def _render(markup: str) -> pymupdf.Document:
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=300)
    page.insert_htmlbox(
        pymupdf.Rect(20, 20, 280, 280),
        to_html(markup),
        css=BASE_CSS + "body {font-size: 11pt;}",
        archive=pymupdf.Archive(str(FONT_DIR)),
    )
    return pymupdf.open("pdf", doc.tobytes())


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def test_copied_thai_equals_the_source_and_the_page_looks_the_same():
    plain = re.sub(r"</?[ckb]>", "", SOURCE)
    before = _render(SOURCE)
    assert _squash(before[0].get_text()) != _squash(plain)  # MuPDF alone gets it wrong
    after = _render(SOURCE)
    report = fix_text_layer(after)
    after = pymupdf.open("pdf", after.tobytes())
    text = after[0].get_text()
    assert _squash(text) == _squash(plain)
    assert "แสดงไฟล์ทั้งหมด" in text  # no spaces between Thai words
    assert report.sara_am >= 4 and report.spaces > 0
    pixels = before[0].get_pixmap(dpi=120).samples == after[0].get_pixmap(dpi=120).samples
    assert pixels


def test_fixing_twice_changes_nothing():
    doc = _render(SOURCE)
    fix_text_layer(doc)
    once = doc.tobytes()
    second = fix_text_layer(doc)
    assert second.sara_am == 0 and second.spaces == 0
    assert pymupdf.open("pdf", once)[0].get_text() == doc[0].get_text()


def test_glyph_names_give_the_real_characters():
    by_cmap = {"space": 0x20, "uni0E48.small": 0xF70A}
    assert _unicode_of("uni0E48.small", by_cmap) == chr(0x0E48)  # not the private-use form
    assert _unicode_of("uni0E310E4A", {}) == chr(0x0E31) + chr(0x0E4A)
    assert _unicode_of("space", by_cmap) == " "
    assert _unicode_of("fi", {}) == "fi"  # two letters, not the ligature character
    assert _unicode_of(".notdef", {}) == ""


def test_real_spaces_stay_and_cancelled_spaces_go():
    info = face_info("IBMPlexSansThai-Regular.ttf")
    sp, w = info.space_gid, info.space_width
    items = [("g", 10), ("g", sp), ("n", w), ("g", 11), ("g", sp), ("g", 12), ("g", sp), ("n", -5)]
    out, dropped = _drop_spaces(items, info)
    assert dropped == 1
    assert out == [("g", 10), ("g", 11), ("g", sp), ("g", 12), ("g", sp), ("n", -5)]


def test_rendered_thai_page_is_fixed(tmp_path):
    pdf = pymupdf.open()
    page = pdf.new_page(width=612, height=792)
    page.insert_text((72, 100), "The commands are listed here.", fontsize=11)
    path = tmp_path / "one.pdf"
    pdf.save(path)
    line = pymupdf.open(path)[0].get_text("dict")["blocks"][0]["lines"][0]["bbox"]
    block = {
        "id": 1, "role": "paragraph", "x0": 72, "y0": line[1], "x1": 400, "y1": line[3],
        "style": {"size": 11, "lines": [list(line)]}, "translate": True,
        "markup_th": "คำสั่งที่อยู่ในรายการนี้", "cells_th": None,
    }  # fmt: skip
    doc, _fits = render_thai_page(path, 1, [block])
    assert "คำสั่งที่อยู่ในรายการนี้" in doc[0].get_text()
