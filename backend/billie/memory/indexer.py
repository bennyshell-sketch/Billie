"""Indexing the folders you have explicitly allow-listed.

The rule from the brief: Billie reads the folders you name and nothing else.
There is no "index everything" mode, and a root that is obviously too broad --
a filesystem root, a drive letter, your bare home folder -- is refused with a
reason rather than quietly walked.

Files are keyed as `<root-name>/<relative path>`, which is also how they are
cited back to you, so an answer always says which folder it came from.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from billie.memory.extract import (
    SKIP_DIRECTORIES,
    ExtractionError,
    extract,
    is_supported,
)
from billie.memory.store import MemoryStore

logger = logging.getLogger("billie.indexer")

#: Source type for anything read off disk.
FILE_SOURCE_TYPE = "file"
#: Phase 1 stored notes under this type with un-namespaced ids. Cleaning it up
#: alongside the normal stale sweep keeps an upgraded install from showing the
#: same note twice.
LEGACY_SOURCE_TYPE = "note"


@dataclass
class IndexReport:
    indexed: int = 0
    unchanged: int = 0
    removed: int = 0
    roots: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    refused_roots: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "indexed": self.indexed,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "roots": self.roots,
            "skipped": self.skipped[:50],
            "refused_roots": self.refused_roots,
        }


def refusal_reason(path: Path) -> str | None:
    """Why this folder must not be indexed, or None if it is acceptable."""
    try:
        resolved = path.resolve()
    except OSError as exc:
        return f"cannot be resolved ({exc})"

    if resolved.parent == resolved:
        return "is a filesystem or drive root -- name a specific folder instead"
    if resolved == Path.home():
        return "is your whole home folder -- name the specific folders you want"
    # /, /home, /Users, C:\Users and friends: one level above a home directory.
    if resolved in {Path.home().parent, Path("/"), Path("/home"), Path("/Users")}:
        return "is too broad -- name the specific folders you want"
    return None


def _file_signature(path: Path) -> tuple[float, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime, stat.st_size)


def walk_root(root: Path, *, max_bytes: int) -> tuple[list[Path], list[str]]:
    """Every indexable file under `root`, plus reasons for what was skipped."""
    files: list[Path] = []
    skipped: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune in place so os.walk never descends into them.
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIRECTORIES and not name.startswith(".")
        ]
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            path = Path(dirpath) / filename
            if not is_supported(path):
                continue
            if path.is_symlink():
                skipped.append(f"{path.name}: symlink, not followed")
                continue
            signature = _file_signature(path)
            if signature is None:
                skipped.append(f"{path.name}: unreadable")
                continue
            if signature[1] > max_bytes:
                skipped.append(
                    f"{path.name}: {signature[1] // 1024}KB is over the size limit"
                )
                continue
            files.append(path)
    return sorted(files), skipped


def _title_for(path: Path, text: str) -> str:
    for line in text.splitlines()[:20]:
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()[:120]
        if stripped:
            break
    return path.stem.replace("-", " ").replace("_", " ").strip() or path.name


def index_roots(
    store: MemoryStore,
    roots: list[tuple[str, Path]],
    *,
    max_bytes: int = 8 * 1024 * 1024,
) -> IndexReport:
    """Sync every allow-listed folder into memory: add, update, drop deleted."""
    report = IndexReport()
    seen: set[str] = set()

    for name, root in roots:
        refusal = refusal_reason(root)
        if refusal:
            message = f"{root}: {refusal}"
            report.refused_roots.append(message)
            logger.warning("Refusing to index %s", message)
            continue
        if not root.exists() or not root.is_dir():
            report.skipped.append(f"{root}: folder does not exist")
            continue

        report.roots.append(f"{name} -> {root}")
        files, skipped = walk_root(root, max_bytes=max_bytes)
        report.skipped.extend(skipped)

        for path in files:
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:  # pragma: no cover - walk always stays under root
                continue
            source_id = f"{name}/{relative}"
            try:
                extracted = extract(path)
            except ExtractionError as exc:
                report.skipped.append(f"{source_id}: {exc}")
                continue
            if not extracted.text.strip():
                continue

            seen.add(source_id)
            _, changed = store.upsert_document(
                source_type=FILE_SOURCE_TYPE,
                source_id=source_id,
                title=_title_for(path, extracted.text),
                content=extracted.text,
                metadata={
                    "root": name,
                    "path": str(path),
                    "suffix": path.suffix.lower(),
                    "bytes": len(extracted.text.encode("utf-8")),
                    **extracted.metadata,
                },
            )
            if changed:
                report.indexed += 1
            else:
                report.unchanged += 1

    known = store.document_source_ids(FILE_SOURCE_TYPE) | store.document_source_ids(
        LEGACY_SOURCE_TYPE
    )
    for stale in known - seen:
        if store.delete_document(stale):
            report.removed += 1
    return report


def root_signature(roots: list[tuple[str, Path]], *, max_bytes: int) -> dict[str, tuple]:
    """A cheap snapshot of every indexable file, for change detection.

    Compares paths, modification times, and sizes without reading contents, so
    the watcher can decide whether a re-index is worth doing at all.
    """
    signature: dict[str, tuple] = {}
    for name, root in roots:
        if refusal_reason(root) or not root.exists():
            continue
        files, _ = walk_root(root, max_bytes=max_bytes)
        for path in files:
            file_signature = _file_signature(path)
            if file_signature is not None:
                signature[f"{name}/{path}"] = file_signature
    return signature
