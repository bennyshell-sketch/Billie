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
from billie.memory.notes import IndexReport, index_notes
from billie.memory.store import MemoryStore, RetrievedChunk
from billie.models.base import ChatMessage, Completion, TextDelta
from billie.models.router import ModelRouter, RoutingDecision

MAX_HISTORY_MESSAGES = 20


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
        owner: str = "the user",
    ) -> None:
        self.settings = settings
        self.store = store
        self.router = router
        self.audit = audit
        self.owner = owner

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------
    def reindex_notes(self) -> IndexReport:
        report = index_notes(self.store, self.settings.notes_dir)
        self.audit.record(
            "memory.reindex", folder=str(self.settings.notes_dir), **report.to_dict()
        )
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
        return self.store.search(
            query,
            top_k=top_k or self.settings.retrieval_top_k,
            min_score=self.settings.retrieval_min_score,
        )

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
