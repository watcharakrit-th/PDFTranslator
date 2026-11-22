from pdftr.glossary import default_for, load_seed
from pdftr.review.markup import markup_html, retag


def test_markup_html_renders_our_tags_only():
    html = markup_html("Use <c>ls -l</c> and <k>pipes</k> &lt;script&gt;")
    assert html == 'Use <code>ls -l</code> and <strong class="key">pipes</strong> &lt;script&gt;'


def test_markup_html_escapes_raw_angle_brackets_from_a_pdf():
    # NFKC can turn a full-width '<' from a PDF into a real one: it must stay text
    assert markup_html("a <script>x</script> b") == "a &lt;script&gt;x&lt;/script&gt; b"


def test_retag_restores_tags_in_order():
    old = "Run <c>echo</c> with <k>escape sequences</k>; <c>echo</c> prints."
    new = "Run echo with escape sequences first; then echo prints."
    assert retag(old, new) == (
        "Run <c>echo</c> with <k>escape sequences</k> first; then <c>echo</c> prints."
    )


def test_retag_drops_tags_of_removed_words_and_escapes_text():
    assert retag("Use <c>grep</c> here", "Use a < b here") == "Use a &lt; b here"


def test_seed_has_established_thai_terms_only_for_policy_b():
    seed = load_seed()
    assert seed["database"] == "ฐานข้อมูล"
    assert seed["operating system"] == "ระบบปฏิบัติการ"
    assert "kernel" not in seed and "shell" not in seed  # specialized: stay English


def test_defaults():
    assert default_for("ls", "code", {"code"})["review_en"] == "approved"
    assert default_for("database", "term", {"styled"})["term_th"] == "ฐานข้อมูล"
    assert default_for("subshell", "term", {"styled"})["decision"] == "keep_en"
    assert default_for("press enter", "term", {"ngram"})["decision"] == "ignore"
