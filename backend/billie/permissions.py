"""Per-tool permission grants.

Every capability that touches something outside Billie's own database is named
here, and can be revoked on its own. A revoked tool fails closed: the code path
raises `PermissionDenied` rather than degrading into doing the thing anyway.

Defaults are deliberate rather than uniform:

* `file.read` is granted by default, because naming a folder in
  `BILLIE_INDEXED_FOLDERS` is already the explicit act of consent. Revoking it
  stops indexing and drops file chunks out of retrieval.
* `file.write` is denied by default. Billie cannot write anything until you
  grant it -- and even then, only through a diff you have approved.

* `calendar.read` is granted by default for the same reason: connecting a
  Google account is the consent. `calendar.write` is denied by default, and
  even once granted every change still waits for a per-action confirmation.

* `voice.transcribe` is granted by default because it is purely local.
  `voice.speak` is denied by default: it sends text to ElevenLabs and costs
  money per character. `voice.clone` is denied by default too.

Later phases register their own tools here (browsing, purchases), so a grant in
the UI always corresponds to something that actually exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from billie.audit import AuditLog
from billie.memory.store import MemoryStore

FILE_READ = "file.read"
FILE_WRITE = "file.write"
CALENDAR_READ = "calendar.read"
CALENDAR_WRITE = "calendar.write"
VOICE_TRANSCRIBE = "voice.transcribe"
VOICE_SPEAK = "voice.speak"
VOICE_CLONE = "voice.clone"


@dataclass(frozen=True)
class ToolPermission:
    name: str
    title: str
    description: str
    default_granted: bool


REGISTRY: dict[str, ToolPermission] = {
    FILE_READ: ToolPermission(
        name=FILE_READ,
        title="Read your files",
        description=(
            "Index and search the folders listed in BILLIE_INDEXED_FOLDERS, plus "
            "your notes folder. Revoking this stops indexing and removes file "
            "content from answers."
        ),
        default_granted=True,
    ),
    FILE_WRITE: ToolPermission(
        name=FILE_WRITE,
        title="Write files in the workspace",
        description=(
            "Apply a change to a file in the workspace folder, after you have "
            "seen and approved the exact diff. Billie can never write anywhere "
            "else, and never without your approval."
        ),
        default_granted=False,
    ),
    CALENDAR_READ: ToolPermission(
        name=CALENDAR_READ,
        title="Read your calendar",
        description=(
            "Fetch events from the Google account you connected, so Billie can "
            "answer questions about your schedule and cite the event it read. "
            "Revoking this stops syncing and removes events from answers."
        ),
        default_granted=True,
    ),
    CALENDAR_WRITE: ToolPermission(
        name=CALENDAR_WRITE,
        title="Change your calendar",
        description=(
            "Create, move, or delete events in your Google Calendar -- each one "
            "only after you have seen exactly what will change and approved it."
        ),
        default_granted=False,
    ),
    VOICE_TRANSCRIBE: ToolPermission(
        name=VOICE_TRANSCRIBE,
        title="Transcribe your speech",
        description=(
            "Turn what you say into text using Whisper, running on this machine. "
            "No audio is sent anywhere -- the recording is decoded in memory and "
            "discarded once it has been transcribed."
        ),
        default_granted=True,
    ),
    VOICE_SPEAK: ToolPermission(
        name=VOICE_SPEAK,
        title="Speak replies aloud",
        description=(
            "Send the text of Billie's reply to ElevenLabs under your API key and "
            "play the audio back. This leaves your machine and is billed per "
            "character, so it is off until you turn it on."
        ),
        default_granted=False,
    ),
    VOICE_CLONE: ToolPermission(
        name=VOICE_CLONE,
        title="Create a cloned voice",
        description=(
            "Upload recordings from your voice-samples folder to ElevenLabs to "
            "create a voice under your own account. Only ever your own voice, "
            "and only after you confirm the exact files."
        ),
        default_granted=False,
    ),
}


class PermissionDenied(PermissionError):
    """A tool was used without a grant. Carries the tool name for the API."""

    def __init__(self, tool: str, message: str | None = None) -> None:
        self.tool = tool
        super().__init__(
            message
            or (
                f"Permission '{tool}' is not granted. Grant it in the UI, or with "
                f"POST /api/permissions/{tool}."
            )
        )


class Permissions:
    """Reads and writes grants, and records every change in the audit log."""

    def __init__(self, store: MemoryStore, audit: AuditLog) -> None:
        self.store = store
        self.audit = audit

    def is_granted(self, tool: str) -> bool:
        definition = REGISTRY.get(tool)
        if definition is None:
            raise KeyError(f"Unknown permission {tool!r}")
        stored = self.store.get_permission(tool)
        return definition.default_granted if stored is None else stored["granted"]

    def require(self, tool: str) -> None:
        if not self.is_granted(tool):
            self.audit.record("permission.denied", tool=tool)
            raise PermissionDenied(tool)

    def set(self, tool: str, granted: bool, note: str = "") -> dict[str, Any]:
        if tool not in REGISTRY:
            raise KeyError(f"Unknown permission {tool!r}")
        self.store.set_permission(tool, granted, note)
        self.audit.record(
            "permission.granted" if granted else "permission.revoked",
            tool=tool,
            note=note,
        )
        return self.describe(tool)

    def describe(self, tool: str) -> dict[str, Any]:
        definition = REGISTRY[tool]
        stored = self.store.get_permission(tool)
        return {
            "tool": definition.name,
            "title": definition.title,
            "description": definition.description,
            "granted": self.is_granted(tool),
            "default_granted": definition.default_granted,
            "explicitly_set": stored is not None,
            "updated_at": stored["updated_at"] if stored else None,
            "note": stored["note"] if stored else "",
        }

    def state(self) -> list[dict[str, Any]]:
        return [self.describe(tool) for tool in REGISTRY]
