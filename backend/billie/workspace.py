"""The workspace: the one folder Billie may write to, and only with approval.

Nothing here writes on its own. A change is *proposed*, which produces a
unified diff and touches nothing on disk; you approve or reject it; only an
approval writes. The proposal records a hash of the file as it was when the
diff was generated, so a file that changed underneath you cannot be silently
clobbered by a stale approval.

Every path is confined to the workspace root after symlink resolution, so
`../../.ssh/id_rsa` and a symlink pointing outside are both refused.
"""

from __future__ import annotations

import difflib
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from billie.audit import AuditLog
from billie.memory.store import MemoryStore, content_hash

MAX_WRITE_BYTES = 5 * 1024 * 1024


class WorkspaceError(RuntimeError):
    """Something about the request was wrong. The message is user-facing."""


class PathOutsideWorkspace(WorkspaceError):
    """The path escaped the workspace folder."""


class ProposalConflict(WorkspaceError):
    """The file changed after the diff was generated."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


@dataclass
class Proposal:
    id: str
    path: str
    action: str  # create | update | delete
    diff: str
    note: str
    status: str
    created_at: str
    expires_at: str
    new_content: str | None = None
    base_hash: str = ""
    resolution: str = ""
    resolved_at: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Proposal":
        return cls(
            id=row["id"],
            path=row["path"],
            action=row["action"],
            diff=row["diff"],
            note=row["note"],
            status=row["status"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            new_content=row["new_content"],
            base_hash=row["base_hash"],
            resolution=row["resolution"],
            resolved_at=row["resolved_at"],
        )

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "path": self.path,
            "action": self.action,
            "diff": self.diff,
            "note": self.note,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "resolution": self.resolution,
            "resolved_at": self.resolved_at,
        }
        if include_content:
            payload["new_content"] = self.new_content
        return payload


class Workspace:
    def __init__(
        self,
        root: Path,
        store: MemoryStore,
        audit: AuditLog,
        *,
        backup_dir: Path | None = None,
        ttl_minutes: float = 120.0,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.audit = audit
        self.backup_dir = Path(backup_dir) if backup_dir else self.root.parent / "backups"
        self.ttl_minutes = ttl_minutes

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    def resolve(self, relative_path: str) -> Path:
        """Resolve a workspace-relative path, refusing anything that escapes."""
        candidate = str(relative_path).strip().replace("\\", "/")
        if not candidate:
            raise WorkspaceError("A path is required.")
        if Path(candidate).is_absolute() or candidate.startswith("~"):
            raise PathOutsideWorkspace(
                "Give a path relative to the workspace folder, not an absolute one."
            )

        target = (self.root / candidate).resolve()
        root = self.root.resolve()
        if target != root and root not in target.parents:
            raise PathOutsideWorkspace(
                f"{relative_path!r} is outside the workspace folder. Billie can only "
                "write inside it."
            )
        if target == root:
            raise WorkspaceError("That is the workspace folder itself, not a file.")
        return target

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def list_files(self) -> list[dict[str, Any]]:
        files = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            stat = path.stat()
            files.append(
                {
                    "path": self.relative(path),
                    "bytes": stat.st_size,
                    "modified_at": _iso(
                        datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    ),
                }
            )
        return files

    def read(self, relative_path: str) -> str:
        target = self.resolve(relative_path)
        if not target.exists():
            raise WorkspaceError(f"{relative_path!r} does not exist in the workspace.")
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise WorkspaceError(f"Cannot read {relative_path!r}: {exc}") from exc

    def _current_text(self, target: Path) -> str:
        if not target.exists():
            return ""
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise WorkspaceError(
                f"{self.relative(target)!r} is not a UTF-8 text file, so Billie "
                "will not edit it."
            )

    # ------------------------------------------------------------------
    # Proposing
    # ------------------------------------------------------------------
    def propose_write(self, relative_path: str, content: str, note: str = "") -> Proposal:
        """Prepare a write and return its diff. Nothing is written yet."""
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise WorkspaceError(
                f"That file would be over the {MAX_WRITE_BYTES // (1024 * 1024)}MB "
                "workspace write limit."
            )
        target = self.resolve(relative_path)
        current = self._current_text(target)
        action = "update" if target.exists() else "create"
        if action == "update" and current == content:
            raise WorkspaceError("That file already has exactly this content.")

        path_label = self.relative(target) if target.exists() else str(
            Path(relative_path).as_posix()
        )
        diff = self._diff(current, content, path_label, action)
        return self._store_proposal(
            path=path_label,
            action=action,
            new_content=content,
            diff=diff,
            base_hash=content_hash(current) if target.exists() else "",
            note=note,
        )

    def propose_delete(self, relative_path: str, note: str = "") -> Proposal:
        target = self.resolve(relative_path)
        if not target.exists():
            raise WorkspaceError(f"{relative_path!r} does not exist in the workspace.")
        current = self._current_text(target)
        path_label = self.relative(target)
        diff = self._diff(current, "", path_label, "delete")
        return self._store_proposal(
            path=path_label,
            action="delete",
            new_content=None,
            diff=diff,
            base_hash=content_hash(current),
            note=note,
        )

    @staticmethod
    def _diff(before: str, after: str, path: str, action: str) -> str:
        lines = list(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{path}" if action != "create" else "/dev/null",
                tofile=f"b/{path}" if action != "delete" else "/dev/null",
                n=3,
            )
        )
        if not lines:
            return f"(no textual change to {path})"
        return "".join(line if line.endswith("\n") else line + "\n" for line in lines)

    def _store_proposal(self, **fields: Any) -> Proposal:
        created = _now()
        proposal = Proposal(
            id=f"wp_{uuid.uuid4().hex[:16]}",
            status="pending",
            created_at=_iso(created),
            expires_at=_iso(created + timedelta(minutes=self.ttl_minutes)),
            **fields,
        )
        self.store.create_proposal(
            {
                "id": proposal.id,
                "path": proposal.path,
                "action": proposal.action,
                "new_content": proposal.new_content,
                "diff": proposal.diff,
                "base_hash": proposal.base_hash,
                "note": proposal.note,
                "status": proposal.status,
                "created_at": proposal.created_at,
                "expires_at": proposal.expires_at,
            }
        )
        self.audit.record(
            "workspace.proposed",
            proposal_id=proposal.id,
            path=proposal.path,
            action=proposal.action,
            diff_lines=proposal.diff.count("\n"),
            note=proposal.note,
        )
        return proposal

    # ------------------------------------------------------------------
    # Resolving
    # ------------------------------------------------------------------
    def get(self, proposal_id: str) -> Proposal:
        row = self.store.get_proposal(proposal_id)
        if row is None:
            raise WorkspaceError(f"No proposal with id {proposal_id!r}.")
        return Proposal.from_row(row)

    def pending(self) -> list[Proposal]:
        self._expire_stale()
        return [Proposal.from_row(row) for row in self.store.list_proposals("pending")]

    def history(self, limit: int = 50) -> list[Proposal]:
        return [Proposal.from_row(row) for row in self.store.list_proposals(limit=limit)]

    def _expire_stale(self) -> None:
        now = _now()
        for row in self.store.list_proposals("pending"):
            if datetime.fromisoformat(row["expires_at"]) < now:
                self.store.resolve_proposal(row["id"], "expired", "not approved in time")
                self.audit.record(
                    "workspace.expired", proposal_id=row["id"], path=row["path"]
                )

    def approve(self, proposal_id: str) -> dict[str, Any]:
        """Apply a proposal. This is the only code path that writes."""
        self._expire_stale()
        proposal = self.get(proposal_id)
        if proposal.status != "pending":
            raise WorkspaceError(
                f"That change was already {proposal.status}; propose it again if you "
                "still want it."
            )

        target = self.resolve(proposal.path)
        current = self._current_text(target)
        current_hash = content_hash(current) if target.exists() else ""
        if current_hash != proposal.base_hash:
            self.store.resolve_proposal(
                proposal_id, "expired", "the file changed after the diff was generated"
            )
            self.audit.record(
                "workspace.conflict", proposal_id=proposal_id, path=proposal.path
            )
            raise ProposalConflict(
                f"{proposal.path!r} changed after this diff was generated, so the "
                "change was not applied. Ask for a fresh diff."
            )

        backup = self._backup(target) if target.exists() else None

        if proposal.action == "delete":
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temporary file and replace, so an interrupted write
            # cannot leave a half-written file behind.
            temporary = target.with_suffix(target.suffix + ".billie-tmp")
            temporary.write_text(proposal.new_content or "", encoding="utf-8")
            temporary.replace(target)

        self.store.resolve_proposal(proposal_id, "approved", "applied")
        self.audit.record(
            "workspace.write",
            proposal_id=proposal_id,
            path=proposal.path,
            action=proposal.action,
            backup=str(backup) if backup else None,
            bytes=len((proposal.new_content or "").encode("utf-8")),
        )
        return {
            "proposal_id": proposal_id,
            "path": proposal.path,
            "action": proposal.action,
            "applied": True,
            "backup": str(backup) if backup else None,
        }

    def reject(self, proposal_id: str, reason: str = "") -> dict[str, Any]:
        proposal = self.get(proposal_id)
        if proposal.status != "pending":
            raise WorkspaceError(f"That change was already {proposal.status}.")
        self.store.resolve_proposal(proposal_id, "rejected", reason or "rejected")
        self.audit.record(
            "workspace.rejected",
            proposal_id=proposal_id,
            path=proposal.path,
            reason=reason,
        )
        return {"proposal_id": proposal_id, "path": proposal.path, "applied": False}

    def _backup(self, target: Path) -> Path:
        """Copy the current file aside before it is overwritten or deleted."""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = _now().strftime("%Y%m%dT%H%M%S")
        safe_name = self.relative(target).replace("/", "__")
        backup = self.backup_dir / f"{safe_name}.{stamp}.bak"
        shutil.copy2(target, backup)
        return backup
