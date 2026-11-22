from pdftr.ingest.cutter import cut
from pdftr.ingest.models import Block

_seq: dict[int, int] = {}


def blk(page, role, text, level=None, marker=None, outline_title=None):
    _seq[page] = _seq.get(page, 0) + 1
    style = {"outline_title": outline_title} if outline_title else {}
    return Block(
        page_no=page,
        seq=_seq[page],
        role=role,
        docling_label=role,
        bbox=(0, 0, 100, 10),
        markup=text,
        text=text,
        level=level,
        marker=marker,
        style=style,
    )


def setup_function(_):
    _seq.clear()


def run(blocks, kinds=None):
    pages = {b.page_no for b in blocks}
    return cut(blocks, kinds or {p: "body" for p in pages}, vocab=set())


def test_paragraph_split_by_page_break_is_one_chunk():
    chunks = run(
        [
            blk(1, "heading", "Shells", level=2),
            blk(1, "paragraph", "The shell reads commands and"),
            blk(1, "footer", "Copyright"),
            blk(2, "header", "Chapter 7"),
            blk(2, "paragraph", "runs them in a subshell."),
        ]
    )
    assert len(chunks) == 1
    assert chunks[0].text == "The shell reads commands and runs them in a subshell."
    assert chunks[0].page_start == 1 and chunks[0].page_end == 2
    assert "joined_pages" in chunks[0].flags
    assert chunks[0].section_path == ["Shells"]


def test_each_bullet_is_a_chunk_with_list_intro_and_parent():
    chunks = run(
        [
            blk(1, "heading", "Hardware", level=2),
            blk(1, "paragraph", "A computer contains the following parts:"),
            blk(1, "list_item", "A processor", level=0, marker="•"),
            blk(1, "list_item", "Fast cache", level=1, marker="-"),
            blk(1, "list_item", "Memory", level=0, marker="•"),
        ]
    )
    items = [c for c in chunks if c.kind == "list_item"]
    assert [c.text for c in items] == ["A processor", "Fast cache", "Memory"]
    assert all("Intro: A computer contains the following parts:" in c.context for c in items)
    assert "Parent item: A processor" in items[1].context
    assert "Parent item" not in items[2].context


def test_multiple_choice_options_stay_with_question():
    chunks = run(
        [
            blk(1, "list_item", "What runs the kernel?", level=0, marker="1."),
            blk(1, "list_item", "Boot loader", level=1, marker="a."),
            blk(1, "list_item", "Shell", level=1, marker="b."),
            blk(1, "list_item", "Which file is compressed?", level=0, marker="2."),
        ]
    )
    assert len(chunks) == 2
    assert chunks[0].kind == "question"
    assert chunks[0].text == "What runs the kernel?\na. Boot loader\nb. Shell"
    assert "mcq" in chunks[0].flags


def test_answer_gets_question_as_context():
    chunks = run(
        [
            blk(1, "list_item", "What runs the kernel?", level=0, marker="1."),
            blk(1, "paragraph", "Answer: c. Boot loader"),
            blk(1, "paragraph", "Explanation: The boot loader loads the kernel."),
        ]
    )
    answers = [c for c in chunks if c.kind == "answer"]
    assert len(answers) == 2
    assert all("Question: What runs the kernel?" in c.context for c in answers)


def test_long_paragraph_is_split_at_sentence_ends():
    sentence = "This sentence has exactly ten words in it for testing. "
    chunks = run([blk(1, "paragraph", (sentence * 50).strip())])  # 500 words
    assert len(chunks) == 2
    assert [c.part for c in chunks] == [0, 1]
    assert all(c.text.endswith("testing.") for c in chunks)
    assert all("split" in c.flags for c in chunks)


def test_tables_kept_but_not_embedded_and_skipped_pages_produce_nothing():
    chunks = run(
        [
            blk(1, "paragraph", "Contents line."),
            blk(2, "table", "| a | b |"),
            blk(2, "code", "ls -l"),
            blk(2, "picture", ""),
        ],
        kinds={1: "toc", 2: "body"},
    )
    assert [c.kind for c in chunks] == ["table", "code"]
    assert chunks[0].embed is False and chunks[1].embed is True


def test_key_terms_section_is_not_embedded():
    chunks = run(
        [
            blk(1, "heading", "Key Terms", level=2),
            blk(1, "list_item", "kernel", level=0, marker="•"),
        ]
    )
    assert chunks[0].embed is False
    assert "key_terms_list" in chunks[0].flags


def test_caption_between_page_parts_does_not_break_paragraph():
    chunks = run(
        [
            blk(1, "paragraph", "Linux distributions differ in"),
            blk(2, "caption", "Figure 1-3 A desktop"),
            blk(2, "paragraph", "their default software."),
        ]
    )
    texts = [c.text for c in chunks]
    assert "Linux distributions differ in their default software." in texts


def test_paragraph_after_question_options_is_not_joined_to_them():
    chunks = run(
        [
            blk(1, "list_item", "What runs the kernel?", level=0, marker="1."),
            blk(1, "list_item", "Boot loader", level=1, marker="a."),
            blk(2, "paragraph", "next, the kernel loads its modules."),
        ]
    )
    assert [c.text for c in chunks] == [
        "What runs the kernel?\na. Boot loader",
        "next, the kernel loads its modules.",
    ]


def test_review_question_without_question_mark_keeps_its_options():
    chunks = run(
        [
            blk(1, "heading", "Review Questions", level=2),
            blk(
                1,
                "list_item",
                "Which three partitions are created? (Choose three answers.)",
                level=0,
                marker="18.",
            ),
            blk(1, "list_item", "/", level=1, marker="a."),
            blk(1, "list_item", "/boot", level=1, marker="b."),
            blk(
                1,
                "list_item",
                "Live media gives a full Linux system before installing.",
                level=0,
                marker="19.",
            ),
            blk(1, "list_item", "True", level=1, marker="a."),
            blk(1, "list_item", "False", level=1, marker="b."),
        ]
    )
    assert [(c.kind, c.text) for c in chunks] == [
        ("question", "Which three partitions are created? (Choose three answers.)\na. /\nb. /boot"),
        ("question", "Live media gives a full Linux system before installing.\na. True\nb. False"),
    ]
    assert "Question:" not in chunks[1].context


def test_lettered_steps_in_a_project_stay_separate():
    chunks = run(
        [
            blk(1, "heading", "Project 2-1", level=3),
            blk(1, "list_item", "Install these packages:", level=0, marker="3."),
            blk(1, "list_item", "gcc", level=1, marker="a."),
            blk(1, "list_item", "make", level=1, marker="b."),
        ]
    )
    assert [c.text for c in chunks] == ["Install these packages:", "gcc", "make"]
