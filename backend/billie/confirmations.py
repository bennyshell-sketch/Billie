"""Actions that need an explicit "yes, do it" before they happen.

The rule from the brief: anything irreversible or external-facing -- sending an
email, booking travel, spending money, changing a calendar -- waits for your
confirmation, and shows you exactly what it will do first.

Phase 2 built that for file writes, where the preview is a diff. This is the
same idea for everything else: an action is *proposed* with a human-readable
summary and preview, and nothing reaches the outside world until it is
approved. The executor -- the function that actually performs the action -- is
supplied by whichever integration owns the action, so this queue never needs to
know what a calendar event or a flight booking is.

Phase 7's travel booking uses this queue too.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from billie.audit import AuditLog
from billie.memory.store import MemoryStore

Executor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ConfirmationError(RuntimeError):
    """User-facing: the message explains what to do instead."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


@dataclass
class PendingAction:
    id: str
    kind: str
    summary: str
    preview: str
    payload: dict[str, Any]
    status: str
    created_at: str
    expires_at: str
    resolution: str = ""
    result: dict[str, Any] | None = None
    resolved_at: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "PendingAction":
        return cls(
            id=row["id"],
            kind=row["kind"],
            summary=row["summary"],
            preview=row["preview"],
            payload=json.loads(row["payload"] or "{}"),
            status=row["status"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            resolution=row["resolution"],
            result=json.loads(row["result"] or "{}"),
            resolved_at=row["resolved_at"],
        )

    def to_dict(self, *, include_payload: bool = False) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "kind": self.kind,
            "summary": self.summary,
            "preview": self.preview,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "resolution": self.resolution,
            "result": self.result,
            "resolved_at": self.resolved_at,
        }
        if include_payload:
            payload["payload"] = self.payload
        return payload


class ConfirmationQueue:
    def __init__(
        self, store: MemoryStore, audit: AuditLog, *, ttl_minutes: float = 120.0
    ) -> None:
        self.store = store
        self.audit = audit
        self.ttl_minutes = ttl_minutes
        self._executors: dict[str, Executor] = {}

    def register(self, kind: str, executor: Executor) -> None:
        """Say how an action of this kind is carried out once approved."""
        self._executors[kind] = executor

    # ------------------------------------------------------------------
    def propose(
        self, kind: str, *, summary: str, preview: str, payload: dict[str, Any]
    ) -> PendingAction:
        if kind not in self._executors:
            raise ConfirmationError(f"No handler is registered for {kind!r}.")
        created = _now()
        action = PendingAction(
            id=f"pa_{uuid.uuid4().hex[:16]}",
            kind=kind,
            summary=summary,
            preview=preview,
            payload=payload,
            status="pending",
            created_at=_iso(created),
            expires_at=_iso(created + timedelta(minutes=self.ttl_minutes)),
        )
        self.store.create_action(
            {
                "id": action.id,
                "kind": action.kind,
                "summary": action.summary,
                "preview": action.preview,
                "payload": json.dumps(action.payload),
                "status": action.status,
                "created_at": action.created_at,
                "expires_at": action.expires_at,
            }
        )
        self.audit.record(
            "confirmation.proposed", action_id=action.id, kind=kind, summary=summary
        )
        return action

    def get(self, action_id: str) -> PendingAction:
        row = self.store.get_action(action_id)
        if row is None:
            raise ConfirmationError(f"No pending action with id {action_id!r}.")
        return PendingAction.from_row(row)

    def pending(self, kind_prefix: str | None = None) -> list[PendingAction]:
        self._expire_stale()
        return [
            PendingAction.from_row(row)
            for row in self.store.list_actions("pending", kind_prefix)
        ]

    def history(self, kind_prefix: str | None = None, limit: int = 50) -> list[PendingAction]:
        return [
            PendingAction.from_row(row)
            for row in self.store.list_actions(kind_prefix=kind_prefix, limit=limit)
        ]

    def _expire_stale(self) -> None:
        now = _now()
        for row in self.store.list_actions("pending"):
            if datetime.fromisoformat(row["expires_at"]) < now:
                self.store.resolve_action(row["id"], "expired", "not confirmed in time")
                self.audit.record(
                    "confirmation.expired", action_id=row["id"], kind=row["kind"]
                )

    # ------------------------------------------------------------------
    async def approve(self, action_id: str) -> dict[str, Any]:
        """Carry out an action. This is the only path to the outside world."""
        self._expire_stale()
        action = self.get(action_id)
        if action.status != "pending":
            raise ConfirmationError(
                f"That action was already {action.status}; propose it again if you "
                "still want it."
            )
        executor = self._executors.get(action.kind)
        if executor is None:
            raise ConfirmationError(f"No handler is registered for {action.kind!r}.")

        try:
            result = await executor(action.payload)
        except Exception as exc:  # noqa: BLE001 - the integration decides what failure means
            self.store.resolve_action(action_id, "failed", str(exc))
            self.audit.record(
                "confirmation.failed", action_id=action_id, kind=action.kind, error=str(exc)
            )
            raise

        self.store.resolve_action(action_id, "approved", "carried out", result)
        self.audit.record(
            "confirmation.approved",
            action_id=action_id,
            kind=action.kind,
            summary=action.summary,
            result=result,
        )
        return {"action_id": action_id, "kind": action.kind, "applied": True, "result": result}

    def reject(self, action_id: str, reason: str = "") -> dict[str, Any]:
        action = self.get(action_id)
        if action.status != "pending":
            raise ConfirmationError(f"That action was already {action.status}.")
        self.store.resolve_action(action_id, "rejected", reason or "rejected")
        self.audit.record(
            "confirmation.rejected", action_id=action_id, kind=action.kind, reason=reason
        )
        return {"action_id": action_id, "kind": action.kind, "applied": False}
