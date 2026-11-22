from pdftr.ingest.blocks import _reading_order, build_page_blocks, classify_pages, sort_lines
from pdftr.ingest.cutter import cut
from pdftr.ingest.models import Block, DoclingItem, Line, Span


def text_line(text, bbox, size=9.5):
    return Line(bbox=bbox, spans=[Span(text, bbox, "Utopia", size, 0, False, False)])


def test_bullet_glyph_slightly_lower_than_text_stays_first():
    text1 = text_line("An Internet connection via your network card (bridged", (100, 850, 640, 862))
    bullet = text_line("•", (88, 852, 94, 861))  # top edge 2pt lower than the text
    text2 = text_line("mode)", (100, 864, 130, 876))
    assert [ln.text for ln in sort_lines([text1, text2, bullet])] == [
        "•",
        "An Internet connection via your network card (bridged",
        "mode)",
    ]


def test_list_item_marker_is_removed_and_label_heading_demoted():
    items = [
        DoclingItem(
            1, "section_header", "body", (40, 40, 300, 52), "Estimated Time: 50 minutes", 0
        ),
        DoclingItem(1, "list_item", "body", (85, 848, 650, 878), "x", 1, marker="•"),
    ]
    lines = [
        text_line("Estimated Time: 50 minutes", (40, 40, 300, 52)),
        text_line("An Internet connection (bridged", (100, 850, 640, 862)),
        text_line("•", (88, 852, 94, 861)),
        text_line("mode)", (100, 864, 130, 876)),
    ]
    blocks = build_page_blocks(1, (612, 900), items, lines, [], set())
    assert blocks[0].role == "paragraph" and "demoted_heading" in blocks[0].flags
    assert blocks[1].text == "An Internet connection (bridged mode)"


def blk(seq, role, bbox, text="x", flags=None, page=1, level=None):
    return Block(
        page_no=page,
        seq=seq,
        role=role,
        docling_label=role,
        bbox=bbox,
        markup=text,
        text=text,
        flags=flags or [],
        level=level,
    )


def test_blocks_above_a_heading_are_read_before_it():
    # Docling order: summary heading, left column, KEY TERMS heading, right column
    summary = blk(1, "heading", (72, 77, 174, 90), "Summary")
    left = blk(2, "list_item", (72, 115, 317, 170), "left bullet.")
    key_terms = blk(3, "heading", (72, 296, 180, 310), "Key Terms")
    right = blk(4, "list_item", (333, 115, 578, 160), "right bullet.")
    terms = blk(5, "paragraph", (72, 329, 208, 700), "kernel")
    order = _reading_order([summary, left, key_terms, right, terms])
    assert [b.text for b in order] == [
        "Summary",
        "left bullet.",
        "right bullet.",
        "Key Terms",
        "kernel",
    ]


def test_orphan_lines_at_page_top_come_first():
    header = blk(1, "header", (371, 31, 576, 45), "Chapter 1")
    para = blk(2, "paragraph", (72, 106, 403, 257), "The operating system carries out")
    footer = blk(3, "footer", (71, 768, 541, 779), "Copyright")
    orphan = blk(4, "paragraph", (72, 65, 403, 100), "operating system. Then...", ["orphan"])
    order = _reading_order([header, para, footer, orphan])
    assert [b.seq for b in order] == [1, 4, 2, 3]


def test_orphan_continuation_joins_previous_page_paragraph():
    p16 = blk(1, "paragraph", (36, 625, 468, 724), "relay it to the", page=16)
    p17 = blk(1, "paragraph", (72, 65, 403, 100), "operating system. Done.", ["orphan"], page=17)
    chunks = cut([p16, p17], {16: "body", 17: "body"}, vocab=set())
    assert len(chunks) == 1 and chunks[0].text == "relay it to the operating system. Done."


def test_sentences_in_key_terms_section_stay_embedded():
    blocks = [
        blk(1, "heading", (0, 0, 10, 10), "Key Terms", level=2),
        blk(2, "list_item", (0, 20, 10, 30), "Linux is used on servers and IoT devices."),
        blk(3, "paragraph", (0, 40, 10, 50), "application (app) bare metal Beowulf clustering"),
    ]
    chunks = cut(blocks, {1: "body"}, vocab=set())
    assert chunks[0].embed is True
    assert chunks[1].embed is False and "key_terms_list" in chunks[1].flags


def test_only_the_first_page_of_the_cover_bookmark_is_cover():
    outline = [[1, "Cover", 1], [1, "Brief Contents", 5], [1, "Chapter 1: Intro", 15]]
    text = " ".join(["word"] * 80)
    blocks = [
        blk(1, "paragraph", (0, 0, 1, 1), text, page=2),
        blk(1, "paragraph", (0, 0, 1, 1), "Due to electronic rights restrictions, ...", page=3),
        blk(1, "paragraph", (0, 0, 1, 1), "ISBN 978-0-357", page=4),
        blk(1, "paragraph", (0, 0, 1, 1), text, page=15),
    ]
    kinds = classify_pages(1, 15, 800, outline, blocks)
    assert kinds[1] == "blank" or kinds[1] == "cover"
    assert kinds[2] == "body" and kinds[3] == "copyright" and kinds[4] == "copyright"
    assert kinds[5] == "toc" and kinds[15] == "body"


def test_list_in_a_second_column_is_not_nested_under_the_first():
    from pdftr.ingest.blocks import assign_list_levels

    q18 = blk(1, "list_item", (70, 400, 330, 430), "Which three partitions?")
    opt = blk(2, "list_item", (88, 440, 140, 450), "/")
    q19 = blk(3, "list_item", (366, 230, 630, 260), "Live media is a full system.")
    true = blk(4, "list_item", (384, 270, 430, 280), "True")
    q20 = blk(5, "list_item", (70, 600, 330, 620), "Which is not virtualization?")
    blocks = [q18, opt, q19, true, q20]
    assign_list_levels(blocks)
    assert [b.level for b in blocks] == [0, 1, 0, 1, 0]
