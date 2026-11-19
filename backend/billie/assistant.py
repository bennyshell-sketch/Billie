"""The assistant service: retrieve, route, answer, record.

One request goes through these steps:

    retrieve from memory  ->  route (local / cloud)  ->  stream the answer
    ->  score how grounded it was  ->  persist and audit

Everything the UI shows about an answer -- which model produced it, why that
model was chosen, what it cited, whether it was grounded -- comes from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator

from billie import __version__
from billie.audit import AuditLog
from billie.config import Settings
from billie.grounding import build_system_prompt, build_user_content, evaluate
from billie.confirmations import ConfirmationQueue
from billie.integrations.calendar_sync import (
    CALENDAR_SOURCE_TYPE,
    SyncReport,
    sync_calendar,
)
from billie.integrations.google_calendar import GoogleCalendar
from billie.memory.indexer import FILE_SOURCE_TYPE, IndexReport, index_roots
from billie.memory.store import MemoryStore, RetrievedChunk
from billie.models.base import ChatMessage, Completion, TextDelta
from billie.models.router import ModelRouter, RoutingDecision
from billie.permissions import (
    CALENDAR_READ,
    CALENDAR_WRITE,
    FILE_READ,
    FILE_WRITE,
    Permissions,
)
from billie.voice.service import VoiceService
from billie.workspace import Workspace

MAX_HISTORY_MESSAGES = 20

#: Everything that can end up in memory. Retrieval is filtered down to the
#: subset the current permission grants allow.
ALL_SOURCE_TYPES = ("fact", "conversation", FILE_SOURCE_TYPE, CALENDAR_SOURCE_TYPE)


@dataclass
class AnswerChunk:
    """A streamed piece of an answer, or its final metadata."""

    type: str  # "start" | "delta" | "final" | "error"
    data: dict[str, Any]


class Assistant:
    def __init__(
        self,
        *,
        settings: Settings,
        store: MemoryStore,
        router: ModelRouter,
        audit: AuditLog,
        permissions: Permissions | None = None,
        workspace: Workspace | None = None,
        confirmations: ConfirmationQueue | None = None,
        calendar: GoogleCalendar | None = None,
        voice: VoiceService | None = None,
        owner: str = "the user",
    ) -> None:
        self.settings = settings
        self.store = store
        self.router = router
        self.audit = audit
        self.owner = owner
        self.permissions = permissions or Permissions(store, audit)
        self.workspace = workspace or Workspace(
            settings.workspace_dir,
            store,
            audit,
            backup_dir=settings.backup_dir,
            ttl_minutes=settings.proposal_ttl_minutes,
        )
        self.confirmations = confirmations or ConfirmationQueue(
            store, audit, ttl_minutes=settings.proposal_ttl_minutes
        )
        #: None until a Google client is configured in .env.
        self.calendar = calendar
        #: Always present; its parts report themselves as unavailable instead.
        self.voice = voice

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------
    def reindex_files(self) -> IndexReport:
        """Re-index every allow-listed folder. Needs the file.read grant."""
        self.permissions.require(FILE_READ)
        report = index_roots(
            self.store,
            self.settings.index_roots(),
            max_bytes=self.settings.max_indexed_file_bytes,
        )
        self.audit.record("memory.reindex", **report.to_dict())
        return report

    def teach(self, content: str, title: str | None = None, tags: list[str] | None = None) -> dict:
        """Store a fact you have taught Billie directly."""
        title = title or content.strip().splitlines()[0][:60]
        source_id = f"fact:{title.lower().replace(' ', '-')[:48]}"
        document_id, changed = self.store.upsert_document(
            source_type="fact",
            source_id=source_id,
            title=title,
            content=content,
            metadata={"tags": tags or [], "taught": True},
        )
        self.audit.record("memory.teach", document_id=document_id, title=title, changed=changed)
        return {"document_id": document_id, "source_id": source_id, "title": title, "changed": changed}

    def search(self, query: str, top_k: int | None = None) -> list[RetrievedChunk]:
        """Retrieve from memory, honouring the file.read grant.

        With file.read revoked, facts you taught Billie directly are still
        available -- they live in Billie's own database, not on your disk --
        but nothing read from a file is returned. The same goes for
        calendar.read and synced events.
        """
        blocked = set()
        if not self.permissions.is_granted(FILE_READ):
            blocked.add(FILE_SOURCE_TYPE)
        if not self.permissions.is_granted(CALENDAR_READ):
            blocked.add(CALENDAR_SOURCE_TYPE)
        source_types = (
            None if not blocked else [k for k in ALL_SOURCE_TYPES if k not in blocked]
        )
        return self.store.search(
            query,
            top_k=top_k or self.settings.retrieval_top_k,
            min_score=self.settings.retrieval_min_score,
            source_types=source_types,
        )

    # ------------------------------------------------------------------
    # Calendar
    # ------------------------------------------------------------------
    async def sync_calendar(self) -> SyncReport:
        """Pull calendar events into memory. Needs the calendar.read grant."""
        self.permissions.require(CALENDAR_READ)
        if self.calendar is None:
            raise RuntimeError(
                "Google Calendar is not configured. Add GOOGLE_CLIENT_ID and "
                "GOOGLE_CLIENT_SECRET to .env."
            )
        report = await sync_calendar(
            self.calendar,
            self.store,
            days_past=self.settings.calendar_days_past,
            days_ahead=self.settings.calendar_days_ahead,
        )
        self.audit.record("calendar.sync", **report.to_dict())
        return report

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------
    def _history(self, conversation_id: str) -> list[ChatMessage]:
        rows = self.store.get_messages(conversation_id, limit=MAX_HISTORY_MESSAGES)
        return [
            ChatMessage(role=row["role"], content=row["content"])
            for row in rows
            if row["role"] in ("user", "assistant") and row["content"].strip()
        ]

    async def answer_stream(
        self,
        message: str,
        *,
        conversation_id: str | None = None,
        route: str | None = None,
        private: bool = False,
        use_memory: bool = True,
        max_tokens: int | None = None,
    ) -> AsyncIterator[AnswerChunk]:
        conversation_id = self.store.ensure_conversation(conversation_id, message)
        history = self._history(conversation_id)

        chunks = self.search(message) if use_memory else []
        decision: RoutingDecision = await self.router.decide(
            message,
            route=route,  # type: ignore[arg-type]
            private=private,
            history_length=len(history),
            context_chunks=len(chunks),
        )

        self.audit.record(
            "chat.request",
            conversation_id=conversation_id,
            private=private,
            route_requested=route or self.settings.default_route,
            routed_to=decision.kind,
            provider=decision.provider,
            model=decision.model,
            reason=decision.reason,
            sources=[chunk.citation for chunk in chunks],
            chars=len(message),
        )

        self.store.add_message(
            conversation_id, "user", message, {"private": private, "route": route}
        )

        yield AnswerChunk(
            "start",
            {
                "conversation_id": conversation_id,
                "routing": decision.to_dict(),
                "sources": [chunk.to_dict() for chunk in chunks],
            },
        )

        system = build_system_prompt(self.owner)
        turn = ChatMessage(role="user", content=build_user_content(message, chunks, self.owner))
        messages = history + [turn]

        completion: Completion | None = None
        async for event in self.router.stream(decision, system, messages, max_tokens=max_tokens):
            if isinstance(event, TextDelta):
                yield AnswerChunk("delta", {"text": event.text})
            elif isinstance(event, Completion):
                completion = event

        if completion is None:
            yield AnswerChunk("error", {"error": "The model returned no answer."})
            return

        grounding = evaluate(completion.text, chunks)
        metadata = {
            "routing": decision.to_dict(),
            "grounding": grounding.to_dict(),
            "usage": completion.usage,
            "latency_ms": completion.latency_ms,
            "sources": [chunk.to_dict() for chunk in chunks],
        }
        message_id = self.store.add_message(
            conversation_id, "assistant", completion.text, metadata
        )
        self.audit.record(
            "chat.response",
            conversation_id=conversation_id,
            message_id=message_id,
            provider=completion.provider,
            model=completion.model,
            kind=decision.kind,
            confidence=grounding.confidence,
            citations=[citation["citation"] for citation in grounding.citations],
            usage=completion.usage,
            latency_ms=completion.latency_ms,
        )

        yield AnswerChunk(
            "final",
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "answer": completion.text,
                "routing": decision.to_dict(),
                "grounding": grounding.to_dict(),
                "sources": [chunk.to_dict() for chunk in chunks],
                "usage": completion.usage,
                "latency_ms": completion.latency_ms,
            },
        )

    async def answer(self, message: str, **kwargs) -> dict[str, Any]:
        """Non-streaming wrapper. Returns the payload of the `final` chunk."""
        final: dict[str, Any] | None = None
        error: str | None = None
        async for chunk in self.answer_stream(message, **kwargs):
            if chunk.type == "final":
                final = chunk.data
            elif chunk.type == "error":
                error = chunk.data.get("error")
        if final is None:
            raise RuntimeError(error or "No answer produced")
        return final

    # ------------------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        providers = await self.router.availability()
        return {
            "version": __version__,
            "providers": providers,
            "memory": self.store.stats(),
            "permissions": self.permissions.state(),
            "files": {
                "roots": [
                    {"name": name, "path": str(path), "exists": path.exists()}
                    for name, path in self.settings.index_roots()
                ],
                "workspace": str(self.settings.workspace_dir),
                "workspace_writable": self.permissions.is_granted(FILE_WRITE),
                "pending_proposals": len(self.workspace.pending()),
                "watch_interval_seconds": self.settings.watch_interval_seconds,
            },
            "calendar": self._calendar_status(),
            "voice": self.voice.status() if self.voice else {"available": False},
            "routing": {
                "default_route": self.settings.default_route,
                "complexity_threshold": self.settings.complexity_threshold,
                "notes_dir": str(self.settings.notes_dir),
            },
            "auth": {
                "token_configured": bool(self.settings.api_token),
                "loopback_unauthenticated": self.settings.allow_loopback_unauthenticated,
            },
        }

    def _calendar_status(self) -> dict[str, Any]:
        if self.calendar is None:
            return {
                "configured": False,
                "connected": False,
                "detail": (
                    "Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to .env to "
                    "connect a Google account."
                ),
                "pending_actions": len(self.confirmations.pending("calendar.")),
                "writable": self.permissions.is_granted(CALENDAR_WRITE),
            }
        connection = self.calendar.oauth.connection()
        return {
            **connection,
            "pending_actions": len(self.confirmations.pending("calendar.")),
            "writable": self.permissions.is_granted(CALENDAR_WRITE),
            "events_in_memory": len(self.store.list_documents(CALENDAR_SOURCE_TYPE)),
        }
