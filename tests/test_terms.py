from pdftr.ingest.models import Block, Chunk, Line, Span
from pdftr.ingest.terms import collect_terms, key_term_lines, normalize


def line(text, x0, y0):
    bbox = (x0, y0, x0 + 150, y0 + 10)
    return Line(bbox=bbox, spans=[Span(text, bbox, "Area", 9, 0, False, False)])


def test_key_term_lines_join_hanging_indent_and_parentheses():
    block = Block(
        page_no=47,
        seq=1,
        role="paragraph",
        docling_label="text",
        bbox=(248, 329, 402, 400),
        markup="",
        text="",
    )
    chunk = Chunk(
        kind="paragraph",
        blocks=[block],
        text="",
        section_path=["Key Terms"],
        flags=["key_terms_list"],
    )
    lines = {
        47: [
            line("GNU Network Object Model", 248, 330),
            line("Environment (GNOME)", 256, 342),
            line("GNU Project", 248, 354),
            line("(GPL)", 248, 366),
        ]
    }
    assert key_term_lines([chunk], lines) == [
        (47, "GNU Network Object Model Environment (GNOME)"),
        (47, "GNU Project (GPL)"),
    ]


def test_display_form_is_lowercase_unless_always_capitalized():
    blocks = [
        body(1, "<k>Software</k> runs on <k>Linux</k>.", "Software runs on Linux."),
        body(2, "The <k>software</k> on <k>Linux</k> is free.", "The software on Linux is free."),
    ]
    terms = {t.norm: t for t in collect_terms(blocks, [], skip_pages=set())}
    assert terms["software"].term == "software"
    assert terms["linux"].term == "Linux"


def body(page, markup, text):
    return Block(
        page_no=page,
        seq=1,
        role="paragraph",
        docling_label="text",
        bbox=(0, 0, 1, 1),
        markup=markup,
        text=text,
    )


def test_normalize_singularizes_and_keeps_acronyms():
    assert normalize("Escape Sequences") == "escape sequence"
    assert normalize("directories") == "directory"
    assert normalize("CPU") == "CPU"
    assert normalize("process") == "process"


def test_collects_styled_terms_acronyms_and_commands():
    blocks = [
        body(
            3,
            "Use <k>escape sequences</k> with <c>echo</c> on the CPU.",
            "Use escape sequences with echo on the CPU.",
        ),
        body(
            4,
            "An escape sequence starts with a backslash; the CPU ignores it.",
            "An escape sequence starts with a backslash; the CPU ignores it.",
        ),
    ]
    terms = {t.norm: t for t in collect_terms(blocks, [], skip_pages=set())}
    esc = terms["escape sequence"]
    assert esc.kind == "term" and "styled" in esc.sources
    assert esc.freq == 2 and esc.first_page == 3
    assert terms["CPU"].kind == "acronym" and terms["CPU"].freq == 2
    assert terms["echo"].kind == "code"
