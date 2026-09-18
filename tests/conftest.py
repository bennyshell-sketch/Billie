"""Shared fixtures.

Nothing in the test suite touches the network, the real notes folder, or the
real database: every test gets a temporary data directory and fake providers.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import AsyncIterator

import pytest

from billie.assistant import Assistant
from billie.audit import AuditLog
from billie.config import Settings
from billie.memory.embeddings import HashingEmbedder
from billie.memory.store import MemoryStore
from billie.models.base import (
    ChatMessage,
    Completion,
    LLMProvider,
    ProviderUnavailable,
    StreamEvent,
    TextDelta,
)
from billie.models.router import ModelRouter

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeProvider(LLMProvider):
    """A scriptable stand-in for Anthropic/Ollama."""

    def __init__(
        self,
        name: str,
        kind: str,
        model: str,
        *,
        reply: str = "Fake answer.",
        healthy: bool = True,
        fail_with: Exception | None = None,
    ) -> None:
        self.name = name
        self.kind = kind  # type: ignore[assignment]
        self.model = model
        self.reply = reply
        self.healthy = healthy
        self.fail_with = fail_with
        self.calls: list[list[ChatMessage]] = []
        self.systems: list[str] = []

    def unavailable_reason(self) -> str | None:
        return None if self.healthy else "fake provider marked offline"

    async def health(self) -> bool:
        return self.healthy

    async def stream(
        self, system: str, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append(list(messages))
        self.systems.append(system)
        if self.fail_with is not None:
            raise self.fail_with
        for word in self.reply.split(" "):
            yield TextDelta(word + " ")
        yield Completion(
            text=self.reply,
            provider=self.name,
            kind=self.kind,  # type: ignore[arg-type]
            model=self.model,
            usage={"input_tokens": 10, "output_tokens": 5},
            stop_reason="end_turn",
            latency_ms=12,
        )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True)
    return Settings(
        _env_file=None,
        ANTHROPIC_API_KEY="test-key-not-real",
        BILLIE_DATA_DIR=str(data_dir),
        BILLIE_NOTES_DIR=str(notes_dir),
        BILLIE_API_TOKEN=None,
        BILLIE_COMPLEXITY_THRESHOLD=0.45,
    )


@pytest.fixture
def store(settings: Settings) -> MemoryStore:
    settings.ensure_dirs()
    memory = MemoryStore(settings.db_path, HashingEmbedder())
    yield memory
    memory.close()


@pytest.fixture
def cloud() -> FakeProvider:
    return FakeProvider("anthropic", "cloud", "claude-opus-5", reply="Cloud answer.")


@pytest.fixture
def local() -> FakeProvider:
    return FakeProvider("ollama", "local", "llama3.1:8b", reply="Local answer.")


@pytest.fixture
def audit(settings: Settings) -> AuditLog:
    settings.ensure_dirs()
    return AuditLog(settings.audit_log_path)


@pytest.fixture
def router(cloud: FakeProvider, local: FakeProvider, settings: Settings) -> ModelRouter:
    return ModelRouter(
        cloud=cloud,
        local=local,
        default_route=settings.default_route,
        complexity_threshold=settings.complexity_threshold,
    )


@pytest.fixture
def assistant(
    settings: Settings, store: MemoryStore, router: ModelRouter, audit: AuditLog
) -> Assistant:
    return Assistant(
        settings=settings, store=store, router=router, audit=audit, owner="Ben"
    )


@pytest.fixture
def client(assistant: Assistant, settings: Settings):
    from fastapi.testclient import TestClient

    from billie.app import create_app

    app = create_app(settings, assistant, serve_frontend=False)
    # client=... makes the request look like it came from this machine,
    # which is what the loopback auth exemption keys off.
    with TestClient(app, client=("127.0.0.1", 51234)) as test_client:
        yield test_client


__all__ = ["FakeProvider", "ProviderUnavailable", "PROJECT_ROOT"]
