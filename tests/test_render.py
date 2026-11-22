import pymupdf

from pdftr.render.thai_page import ZWSP, render_thai_page, to_html, word_breaks


def test_word_breaks_between_thai_words_only():
    out = word_breaks("ระบบปฏิบัติการ Linux")
    assert ZWSP not in "".join(ch for ch in out if ch.isascii())
    assert out.replace(ZWSP, "") == "ระบบปฏิบัติการ Linux"


def test_to_html_keeps_code_unbroken():
    html = to_html("ใช้คำสั่ง <c>ls -l</c> เพื่อดู")
    assert "<code>ls -l</code>" in html


def test_render_replaces_english_with_thai(tmp_path):
    pdf = pymupdf.open()
    page = pdf.new_page(width=612, height=792)
    page.insert_text((72, 100), "The shell reads commands.", fontsize=11)
    path = tmp_path / "one.pdf"
    pdf.save(path)
    line = pymupdf.open(path)[0].get_text("dict")["blocks"][0]["lines"][0]["bbox"]
    block = {
        "id": 1,
        "role": "paragraph",
        "x0": 72,
        "y0": line[1],
        "x1": 400,
        "y1": line[3],
        "style": {"size": 11, "lines": [list(line)]},
        "translate": True,
        "markup_th": "เชลล์อ่านคำสั่ง",
        "cells_th": None,
    }
    doc, fits = render_thai_page(path, 1, [block])
    text = doc[0].get_text()
    assert "commands" not in text  # the English is gone
    assert len(fits) == 1 and fits[0].overflow is False
    assert any("Thai" in f[3] for f in doc[0].get_fonts())
