from pdftr.ingest.textutil import join_lines, split_sentences, strip_markup


def line(text, starts_code=False, ends_code=False):
    return (text, starts_code, ends_code)


def test_line_end_hyphen_removed_when_joined_word_is_known():
    vocab = {"computer", "a", "is"}
    assert join_lines([line("a com-"), line("puter is")], vocab) == "a computer is"


def test_line_end_hyphen_kept_for_real_compound():
    vocab = {"three", "digit", "number", "octal"}
    assert join_lines([line("a three-"), line("digit number")], vocab) == "a three-digit number"


def test_unknown_hyphenation_is_removed():
    assert join_lines([line("the trans-"), line("lation")], set()) == "the translation"


def test_code_wrapped_over_lines_gets_no_space():
    out = join_lines(
        [
            line("the <c>. ./</c>", ends_code=True),
            line("<c>myscript</c> command", starts_code=True),
        ],
        set(),
    )
    assert out == "the <c>. ./myscript</c> command"


def test_sentence_split_respects_abbreviations_and_versions():
    text = "Use tools, e.g. grep. Version 6.13.8 is stable. Next one follows."
    assert split_sentences(text) == [
        "Use tools, e.g. grep.",
        "Version 6.13.8 is stable.",
        "Next one follows.",
    ]


def test_strip_markup_decodes_escaped_text():
    assert strip_markup("run <c>cat &lt; file</c> and <k>pipes</k>") == "run cat < file and pipes"
