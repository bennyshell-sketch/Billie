"""Append-only local audit log.

Every external API call, tool use, and (from phase 2) file write is recorded
here so it can be reviewed. The log is a JSON Lines file on disk -- readable
with `tail -f`, greppable, and never sent anywhere.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Anything that looks like a credential is redacted before it is written.
_SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|token|secret|password|authorization|credential)", re.IGNORECASE
)
_SECRET_VALUE_PATTERN = re.compile(r"\b(sk-[A-Za-z0-9\-_]{12,}|Bearer\s+\S+)")
REDACTED = "[redacted]"


def _redact(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SECRET_KEY_PATTERN.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE_PATTERN.sub(REDACTED, value)
    return value


class AuditLog:
    """Thread-safe append-only JSONL audit log."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, event: str, **detail: Any) -> dict[str, Any]:
        """Append one event. Returns the entry that was written."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            "detail": _redact(detail),
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return entry

    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the most recent entries, newest last."""
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        entries: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
