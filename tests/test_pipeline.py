from pdftr.ingest.pipeline import document_title


def test_document_title_prefers_a_readable_title():
    assert document_title("Linux Basics", "file") == "Linux Basics"
    stem = "CompTIA Linux+ LPIC-1 Guide, 7e (Jason Eckert) (some-site.example)"
    assert document_title("3P-EBK: COMPTIA LINUX+ GUIDE", stem) == "CompTIA Linux+ LPIC-1 Guide, 7e"
    assert document_title(None, "notes (draft)") == "notes"
