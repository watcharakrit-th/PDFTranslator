from pdftr.ingest.pipeline import document_title


def test_document_title_prefers_a_readable_title():
    assert document_title("Linux Basics", "file") == "Linux Basics"
    stem = "CompTIA Linux+ LPIC-1 Guide, 7e (Jason Eckert) (some-site.example)"
    assert document_title("3P-EBK: COMPTIA LINUX+ GUIDE", stem) == "CompTIA Linux+ LPIC-1 Guide, 7e"
    assert document_title(None, "notes (draft)") == "notes"


def test_relocate_follows_a_moved_book_by_its_content(conn, sample_doc, sample_pdf, tmp_path):
    import shutil

    from pdftr.ingest.pipeline import sha256_file
    from pdftr.ingest.store import relocate_documents

    conn.execute("UPDATE documents SET file_hash=%s", (sha256_file(sample_pdf),))
    moved = tmp_path / "server" / "books"
    moved.mkdir(parents=True)
    shutil.copy(sample_pdf, moved / "sample.pdf")
    assert [s for _d, s in relocate_documents(conn, moved)] == ["moved"]
    row = conn.execute("SELECT file_path FROM documents").fetchone()
    assert row["file_path"] == str((moved / "sample.pdf").resolve())
    assert [s for _d, s in relocate_documents(conn, moved)] == ["already here"]
    (moved / "sample.pdf").write_bytes(b"%PDF-1.7 another edition")
    assert [s for _d, s in relocate_documents(conn, moved)] == ["different file"]
    (tmp_path / "empty").mkdir()
    assert [s for _d, s in relocate_documents(conn, tmp_path / "empty")] == ["not found"]
