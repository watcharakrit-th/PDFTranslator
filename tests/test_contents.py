"""Contents entries read from the PDF's own lines."""

import pymupdf

from pdftr.render.contents import entries, key


def _page():
    """Two columns, like the CompTIA contents pages."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    left = [
        (50, 100, "Chapter 1", 14, None),  # a label without a page number
        (50, 125, "Introduction to Linux\x08", 10, "1"),
        (62, 145, "Displaying the Contents", 9.5, None),  # wraps onto the next line
        (62, 158, "of Text Files", 9.5, "89"),
        (50, 185, "Chapter 11", 14, None),  # the 11 is part of the title
        (50, 210, "Linux vs. Windows\t", 10, "xii"),
    ]
    for x, y, text, size, number in left:
        page.insert_text((x, y), text, fontsize=size)
        if number:
            page.insert_text((260, y), number, fontsize=size)
    page.insert_text((320, 100), "Basic Shell Commands", fontsize=10)  # right column, same
    page.insert_text((540, 100), "58", fontsize=10)  # baseline as "Chapter 1"
    return doc[0]


def test_entries_pair_titles_with_their_own_page_numbers():
    found = {e.title: e for e in entries(_page(), (40, 80, 580, 230))}
    assert set(found) == {
        "Chapter 1",
        "Introduction to Linux",
        "Displaying the Contents of Text Files",
        "Chapter 11",
        "Linux vs. Windows",
        "Basic Shell Commands",
    }
    assert found["Chapter 1"].number is None  # not the right column's 58
    assert found["Basic Shell Commands"].number is not None
    assert len(found["Displaying the Contents of Text Files"].rects) == 2
    assert found["Linux vs. Windows"].number is not None  # roman page number after a tab
    intro = found["Introduction to Linux"]
    assert intro.right < intro.number[0] and intro.right > intro.rects[0][2]


def test_lines_of_a_heading_are_left_to_the_heading():
    page = _page()
    titles = {e.title for e in entries(page, (40, 80, 580, 230), exclude=[(40, 85, 300, 105)])}
    assert "Chapter 1" not in titles and "Introduction to Linux" in titles


def test_keys_ignore_case_and_trailing_punctuation():
    assert key("Linux Distributions\x08 ") == key("linux distributions") == "t:linux distributions"
