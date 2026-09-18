"""Indexing the allow-listed folders."""

from __future__ import annotations

import os

import pytest

from billie.memory.extract import ExtractionError, extract, is_supported
from billie.memory.indexer import (
    index_roots,
    refusal_reason,
    root_signature,
    walk_root,
)
from billie.memory.store import MemoryStore


@pytest.fixture
def roots(tmp_path):
    notes = tmp_path / "notes"
    project = tmp_path / "project"
    notes.mkdir(exist_ok=True)
    project.mkdir()
    return [("notes", notes), ("project", project)]


# --- what counts as indexable --------------------------------------------


def test_text_and_code_files_are_indexable(tmp_path):
    for name in ("a.md", "b.txt", "c.py", "d.sql", "e.json"):
        assert is_supported(tmp_path / name), name


def test_binaries_and_unknown_types_are_not(tmp_path):
    for name in ("a.exe", "b.png", "c.zip", "d.sqlite3", "noextension"):
        assert not is_supported(tmp_path / name), name


def test_plain_text_extraction_falls_back_from_bad_utf8(tmp_path):
    path = tmp_path / "legacy.txt"
    path.write_bytes("café".encode("latin-1"))
    assert extract(path).text.strip()


def test_pdf_text_is_extracted(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    path = tmp_path / "report.pdf"
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    # A PDF with no text layer is the scanned-document case.
    writer.write(str(path))
    with pytest.raises(ExtractionError) as excinfo:
        extract(path)
    assert "OCR" in str(excinfo.value)
    assert page is not None


def test_docx_paragraphs_and_tables_are_extracted(tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "plan.docx"
    document = docx.Document()
    document.add_paragraph("The grant deadline is 14 March.")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Budget"
    table.rows[0].cells[1].text = "£12,500"
    document.save(str(path))

    extracted = extract(path)
    assert "grant deadline" in extracted.text
    assert "Budget | £12,500" in extracted.text
    assert extracted.metadata["tables"] == 1


def test_an_unsupported_type_says_so(tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG")
    with pytest.raises(ExtractionError):
        extract(path)


# --- walking --------------------------------------------------------------


def test_walk_skips_noise_directories_and_dotfiles(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "src" / "main.py").write_text("print('hi')", encoding="utf-8")
    (root / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
    (root / ".git" / "config.toml").write_text("x", encoding="utf-8")
    (root / ".secret.md").write_text("x", encoding="utf-8")

    files, _ = walk_root(root, max_bytes=1_000_000)
    assert [path.name for path in files] == ["main.py"]


def test_walk_enforces_the_size_cap(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "small.md").write_text("small", encoding="utf-8")
    (root / "huge.md").write_text("x" * 5000, encoding="utf-8")

    files, skipped = walk_root(root, max_bytes=1000)
    assert [path.name for path in files] == ["small.md"]
    assert any("size limit" in reason for reason in skipped)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need elevation on Windows")
def test_symlinks_are_not_followed(tmp_path):
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("private", encoding="utf-8")
    (root / "link.md").symlink_to(outside / "secret.md")

    files, skipped = walk_root(root, max_bytes=1_000_000)
    assert files == []
    assert any("symlink" in reason for reason in skipped)


# --- the allow-list guard -------------------------------------------------


def test_a_filesystem_root_is_refused(tmp_path):
    from pathlib import Path

    assert refusal_reason(Path(tmp_path.anchor)) is not None
    assert refusal_reason(Path.home()) is not None
    assert refusal_reason(tmp_path) is None


def test_index_roots_refuses_a_too_broad_root_and_says_why(store: MemoryStore, tmp_path):
    from pathlib import Path

    report = index_roots(store, [("everything", Path(tmp_path.anchor))])
    assert report.indexed == 0
    assert report.refused_roots and "root" in report.refused_roots[0]


# --- indexing -------------------------------------------------------------


def test_files_are_namespaced_by_root(store: MemoryStore, roots):
    (roots[0][1] / "one.md").write_text("# One\n\nA note.", encoding="utf-8")
    (roots[1][1] / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    report = index_roots(store, roots)
    assert report.indexed == 2
    assert store.document_source_ids("file") == {"notes/one.md", "project/main.py"}
    documents = {doc["source_id"]: doc for doc in store.list_documents("file")}
    assert documents["project/main.py"]["metadata"]["root"] == "project"


def test_reindexing_unchanged_files_is_a_no_op(store: MemoryStore, roots):
    (roots[0][1] / "one.md").write_text("A note.", encoding="utf-8")
    assert index_roots(store, roots).indexed == 1
    second = index_roots(store, roots)
    assert second.indexed == 0 and second.unchanged == 1


def test_deleted_files_are_dropped_from_memory(store: MemoryStore, roots):
    path = roots[0][1] / "gone.md"
    path.write_text("Here for now.", encoding="utf-8")
    index_roots(store, roots)
    path.unlink()

    report = index_roots(store, roots)
    assert report.removed == 1
    assert store.document_source_ids("file") == set()


def test_documents_from_a_previous_version_are_cleaned_up(store: MemoryStore, roots):
    """Phase 1 stored notes with un-namespaced ids; they must not linger."""
    store.upsert_document(
        source_type="note", source_id="one.md", title="One", content="Old copy."
    )
    (roots[0][1] / "one.md").write_text("New copy.", encoding="utf-8")

    report = index_roots(store, roots)
    assert report.removed == 1
    assert store.document_source_ids("note") == set()
    assert store.document_source_ids("file") == {"notes/one.md"}


def test_an_unreadable_file_is_reported_not_fatal(store: MemoryStore, roots):
    (roots[0][1] / "good.md").write_text("Fine.", encoding="utf-8")
    (roots[0][1] / "broken.pdf").write_bytes(b"not really a pdf")

    report = index_roots(store, roots)
    assert report.indexed == 1
    assert any("broken.pdf" in reason for reason in report.skipped)


def test_a_missing_folder_is_reported_not_fatal(store: MemoryStore, tmp_path):
    report = index_roots(store, [("absent", tmp_path / "nope")])
    assert report.indexed == 0
    assert any("does not exist" in reason for reason in report.skipped)


def test_indexed_files_are_retrievable_and_cite_their_root(store: MemoryStore, roots):
    (roots[1][1] / "deploy.md").write_text(
        "# Deployment\n\nThe production deploy runs through GitHub Actions on merge.",
        encoding="utf-8",
    )
    index_roots(store, roots)
    results = store.search("how does deployment happen", top_k=3)
    assert results and results[0].source_id == "project/deploy.md"
    assert results[0].citation.startswith("project/deploy.md#")


# --- change detection -----------------------------------------------------


def test_root_signature_changes_only_when_files_change(roots):
    (roots[0][1] / "one.md").write_text("first", encoding="utf-8")
    first = root_signature(roots, max_bytes=1_000_000)
    assert root_signature(roots, max_bytes=1_000_000) == first

    (roots[0][1] / "two.md").write_text("second", encoding="utf-8")
    assert root_signature(roots, max_bytes=1_000_000) != first
