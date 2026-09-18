"""Turning a file on disk into plain text for indexing.

Plain text and code files are read directly. PDF and DOCX need a library each;
both are optional, and a missing one produces a clear "install X" message in
the index report rather than a crash or a silently skipped file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Extensions read as plain text. Code counts: being able to ask questions
#: about your own scripts is the point of indexing a project folder.
TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".rst", ".text", ".log", ".csv", ".tsv",
    ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp",
    ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala",
    ".r", ".jl", ".m", ".sql", ".sh", ".bash", ".zsh", ".ps1", ".bat",
    ".html", ".htm", ".css", ".scss", ".xml", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".env.example", ".tex", ".bib",
}
PDF_SUFFIXES = {".pdf"}
DOCX_SUFFIXES = {".docx"}

SUPPORTED_SUFFIXES = TEXT_SUFFIXES | PDF_SUFFIXES | DOCX_SUFFIXES

#: Folders that are never worth indexing, even inside an allow-listed root.
SKIP_DIRECTORIES = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist",
    "build", "target", ".next", ".cache", ".idea", ".vscode", "site-packages",
    ".terraform", "coverage", "htmlcov", ".DS_Store",
}


class ExtractionError(RuntimeError):
    """The file could not be turned into text. The message says why."""


@dataclass
class Extracted:
    text: str
    #: Extra facts worth keeping, e.g. a PDF's page count.
    metadata: dict


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_SUFFIXES


def extract(path: Path) -> Extracted:
    """Read `path` as text. Raises `ExtractionError` with a usable message."""
    suffix = path.suffix.lower()
    if suffix in PDF_SUFFIXES:
        return _extract_pdf(path)
    if suffix in DOCX_SUFFIXES:
        return _extract_docx(path)
    if suffix in TEXT_SUFFIXES:
        return _extract_text(path)
    raise ExtractionError(f"unsupported file type {suffix or '(none)'}")


def _extract_text(path: Path) -> Extracted:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding="latin-1")
        except (OSError, UnicodeDecodeError) as exc:
            raise ExtractionError(f"could not decode as text ({exc})") from exc
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc
    return Extracted(text=text, metadata={})


def _extract_pdf(path: Path) -> Extracted:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ExtractionError("PDF support needs `pip install pypdf`") from exc

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise ExtractionError("PDF is encrypted")
        pages = [page.extract_text() or "" for page in reader.pages]
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf raises a wide range
        raise ExtractionError(f"could not read PDF ({exc})") from exc

    text = "\n\n".join(page.strip() for page in pages if page.strip())
    if not text.strip():
        raise ExtractionError(
            "no extractable text (likely a scan -- OCR would be needed)"
        )
    return Extracted(text=text, metadata={"pages": len(pages)})


def _extract_docx(path: Path) -> Extracted:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ExtractionError("DOCX support needs `pip install python-docx`") from exc

    try:
        document = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001 - python-docx raises a wide range
        raise ExtractionError(f"could not read DOCX ({exc})") from exc

    parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n\n".join(parts)
    if not text.strip():
        raise ExtractionError("document contains no text")
    return Extracted(
        text=text,
        metadata={"paragraphs": len(document.paragraphs), "tables": len(document.tables)},
    )
