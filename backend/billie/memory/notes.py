"""Indexing of the notes folder.

Phase 1 indexes one explicitly configured folder (`BILLIE_NOTES_DIR`). Phase 2
generalises this to an allow-list of folders -- the rule that Billie only ever
reads folders you name, and never the whole drive, is set here and does not
change.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from billie.memory.store import MemoryStore

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".text"}
MAX_FILE_BYTES = 2_000_000


@dataclass
class IndexReport:
    indexed: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.skipped is None:
            self.skipped = []

    def to_dict(self) -> dict:
        return {
            "indexed": self.indexed,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "skipped": self.skipped,
        }


def _title_for(path: Path, text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
        if stripped:
            break
    return path.stem.replace("-", " ").replace("_", " ").strip() or path.name


def index_notes(store: MemoryStore, notes_dir: Path) -> IndexReport:
    """Sync the notes folder into memory: add, update, and drop deleted files."""
    notes_dir = Path(notes_dir)
    report = IndexReport()
    if not notes_dir.exists():
        return report

    seen: set[str] = set()
    for path in sorted(notes_dir.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                report.skipped.append(f"{path.name}: larger than {MAX_FILE_BYTES} bytes")
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.skipped.append(f"{path.name}: {exc}")
            continue
        if not text.strip():
            continue

        source_id = str(path.relative_to(notes_dir).as_posix())
        seen.add(source_id)
        _, changed = store.upsert_document(
            source_type="note",
            source_id=source_id,
            title=_title_for(path, text),
            content=text,
            metadata={"path": str(path), "bytes": len(text.encode("utf-8"))},
        )
        if changed:
            report.indexed += 1
        else:
            report.unchanged += 1

    for stale in store.document_source_ids("note") - seen:
        if store.delete_document(stale):
            report.removed += 1
    return report
