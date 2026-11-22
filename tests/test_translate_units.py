from pdftr.translate.checks import check
from pdftr.translate.protect import placeholder, protect, restore
from pdftr.translate.rules import Term, matching_terms, rules_for, term_pattern
from pdftr.translate.runner import split_markup


def term(en, decision, th=None, kind="term"):
    from pdftr.ingest.terms import normalize

    return Term(en, normalize(en), decision, th, kind, term_pattern(normalize(en)))


def test_code_paths_and_urls_become_placeholders():
    p = protect("Run <c>ls -l</c> in /etc/grub.d or see kernel.org and https://x.org/a.")
    assert p.text == (
        f"Run {placeholder(1)} in {placeholder(4)} or see {placeholder(3)} and {placeholder(2)}."
    )
    assert p.slots[1] == "<c>ls -l</c>"
    assert p.slots[4] == "/etc/grub.d"


def test_and_or_and_tcp_ip_are_not_paths():
    assert protect("Use TCP/IP and/or UDP.").slots == {}


def test_restore_puts_code_back_and_tags_key_terms():
    p = protect("The <c>echo</c> command uses <k>escape sequences</k>.")
    thai = f"คำสั่ง {placeholder(1)} ใช้ escape sequence"
    markup, flags = restore(thai, p, {})
    assert markup == "คำสั่ง <c>echo</c> ใช้ <k>escape sequence</k>"
    assert flags == []


def test_restore_never_loses_code():
    p = protect("Type <c>pwd</c> now.")
    markup, flags = restore("พิมพ์ตอนนี้", p, {})
    assert "placeholder_missing" in flags and markup.endswith("<c>pwd</c>")


def test_translated_key_term_and_label_are_tagged():
    p = protect("<b>Note 1</b> <k>|</k> A <k>database</k> stores data.")
    assert p.label == ("Note", "หมายเหตุ")
    markup, _ = restore("หมายเหตุ 1 | ฐานข้อมูลจัดเก็บข้อมูล", p, {"database": "ฐานข้อมูล"})
    assert markup.startswith("<b>หมายเหตุ 1</b>")
    assert "<k>ฐานข้อมูล</k>" in markup


def test_rules_only_for_terms_in_the_text():
    glossary = [
        term("database", "translate", "ฐานข้อมูล"),
        term("shell script", "keep_en"),
        term("CPU", "keep_en", kind="acronym"),
        term("kernel", "keep_en"),
    ]
    hits = matching_terms("Databases and shell scripts on the CPU", glossary)
    assert [t.term_en for t in hits] == ["shell script", "database"]  # longest first
    rules = rules_for("x", hits, has_placeholders=False)
    assert rules[1:] == ['Keep "shell script" in English.', 'Translate "database" as "ฐานข้อมูล".']


def test_checks_flag_untranslated_informal_and_glossary():
    db = term("database", "translate", "ฐานข้อมูล")
    assert check("A database stores data for you.", "ฐานข้อมูลจัดเก็บข้อมูล", [db]) == []
    assert "untranslated" in check("A database stores data for you.", "A database stores data", [])
    assert "informal" in check("Hello there friend.", "สวัสดีครับเพื่อน", [])
    assert "glossary" in check("A database stores data.", "คลังข้อมูลจัดเก็บข้อมูล", [db])


def test_names_in_parentheses_are_not_untranslated_text():
    assert check("Mainframe (S/390, z/Architecture)", "เมนเฟรม (S/390, z/Architecture)", []) == []
    assert "untranslated" in check("The shell reads every command.", "The shell reads every", [])


def test_thai_spelling_of_a_kept_name_becomes_english_again():
    from pdftr.translate.rules import put_back_kept_names

    linux = term("Linux", "keep_en")
    assert put_back_kept_names("ระบบปฏิบัติการลินุกซ์จะโหลด", [linux]) == ("ระบบปฏิบัติการ Linux จะโหลด")
    assert put_back_kept_names("(ลีนุกซ์)", [linux]) == "(Linux)"
    translated = term("Linux", "translate", th="ลินุกซ์")  # your R3 choice wins
    assert put_back_kept_names("ใช้ลินุกซ์", [translated]) == "ใช้ลินุกซ์"


def test_informal_needs_a_whole_particle():
    assert "informal" not in check("The owner of the file.", "เจ้าของไฟล์", [])  # จ้า inside
    assert "informal" in check("Thank you very much.", "ขอบคุณมากค่ะ", [])


def test_a_term_to_keep_in_english_must_survive():
    linux = term("Linux", "keep_en")
    assert "glossary" in check("Linux is free.", "ลีนุกซ์ใช้ได้ฟรี", [linux])
    assert check("Linux is free.", "ระบบLinuxใช้ได้ฟรี", [linux]) == []  # no space is fine
    unix = term("UNIX", "keep_en", kind="acronym")
    assert "glossary" in check("UNIX came first.", "ยูนิกซ์มาก่อน", [unix])


def test_echoed_rules_after_a_one_line_translation_are_dropped():
    from pdftr.translate.engine import NEWLINE, drop_echo

    def lines(*parts):
        return NEWLINE.join(parts)

    echo = lines("หมายเลขฉบับปรับปรุง", "revision", "revision number")
    assert drop_echo("Revision number", echo) == "หมายเลขฉบับปรับปรุง"
    two_thai = lines("บรรทัดแรก", "บรรทัดที่สอง")
    assert drop_echo("One line.", two_thai) == two_thai  # Thai lines are kept
    assert drop_echo(lines("Two", "lines"), lines("สอง", "lines")) == lines("สอง", "lines")


def test_split_markup_cuts_at_spaces_outside_tags():
    pieces = split_markup("หนึ่ง สอง <c>a b c</c> สาม สี่", [1, 1])
    assert len(pieces) == 2 and "<c>a b c</c>" in "".join(pieces)
    assert all(p.count("<c>") == p.count("</c>") for p in pieces)


def test_long_text_is_split_into_sentence_groups_without_cutting_tags():
    from pdftr.translate.runner import sentence_groups

    sentence = "This sentence has exactly ten words in it for testing. "
    groups = sentence_groups((sentence * 30).strip(), max_words=120)  # 300 words
    assert len(groups) == 3 and all(g.endswith("testing.") for g in groups)
    tricky = "Run <c>echo. Then stop</c> now. " + "Short one here. " * 3
    assert all(g.count("<c>") == g.count("</c>") for g in sentence_groups(tricky, max_words=3))
