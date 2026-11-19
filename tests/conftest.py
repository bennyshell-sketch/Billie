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
from billie.confirmations import ConfirmationQueue
from billie.integrations.google_calendar import GoogleCalendar
from billie.integrations.google_oauth import GoogleOAuth
from billie.integrations.token_store import TokenStore
from billie.models.router import ModelRouter
from billie.permissions import Permissions
from billie.voice.service import VoiceService
from billie.voice.speech import ElevenLabs
from billie.voice.transcribe import Transcriber, Transcription
from billie.workspace import Workspace
from tests.fake_elevenlabs import FakeElevenLabs
from tests.fake_google import FakeGoogle

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
        BILLIE_WORKSPACE_DIR=str(tmp_path / "workspace"),
        BILLIE_VOICE_SAMPLES_DIR=str(tmp_path / "voice-samples"),
        BILLIE_API_TOKEN=None,
        BILLIE_COMPLEXITY_THRESHOLD=0.45,
        # The watcher runs on its own schedule; tests drive it explicitly.
        BILLIE_WATCH_INTERVAL_SECONDS=0,
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
def permissions(store: MemoryStore, audit: AuditLog) -> Permissions:
    return Permissions(store, audit)


@pytest.fixture
def workspace(settings: Settings, store: MemoryStore, audit: AuditLog) -> Workspace:
    settings.ensure_dirs()
    return Workspace(
        settings.workspace_dir,
        store,
        audit,
        backup_dir=settings.backup_dir,
        ttl_minutes=settings.proposal_ttl_minutes,
    )


@pytest.fixture
def confirmations(store: MemoryStore, audit: AuditLog, settings: Settings) -> ConfirmationQueue:
    return ConfirmationQueue(store, audit, ttl_minutes=settings.proposal_ttl_minutes)


@pytest.fixture
def google() -> FakeGoogle:
    """An in-memory Google, with one event already in the diary."""
    fake = FakeGoogle()
    fake.add_event(
        id="evt_standup",
        summary="Team standup",
        start={"dateTime": "2027-03-14T09:00:00+00:00"},
        end={"dateTime": "2027-03-14T09:15:00+00:00"},
        location="Zoom",
    )
    return fake


@pytest.fixture
def token_store(tmp_path) -> TokenStore:
    """The file fallback, so tests do not touch a real keychain."""
    return TokenStore(fallback_path=tmp_path / "credentials.json", allow_file=True)


@pytest.fixture
def oauth(token_store: TokenStore, google: FakeGoogle, audit: AuditLog) -> GoogleOAuth:
    return GoogleOAuth(
        "client-id.apps.googleusercontent.com",
        "client-secret",
        token_store,
        client=google.client(),
        audit=audit,
    )


@pytest.fixture
def connected_oauth(oauth: GoogleOAuth, google: FakeGoogle) -> GoogleOAuth:
    """An OAuth object that already holds a refresh token."""
    oauth.token_store.save(
        oauth.account,
        {
            "refresh_token": "refresh-token-abc",
            "access_token": "access-token-1",
            "expires_at": 4102444800.0,  # far future
            "scopes": list(oauth.scopes),
            "email": google.email,
            "connected_at": "2027-03-01T00:00:00Z",
        },
    )
    return oauth


@pytest.fixture
def calendar(
    connected_oauth: GoogleOAuth,
    confirmations: ConfirmationQueue,
    audit: AuditLog,
    google: FakeGoogle,
) -> GoogleCalendar:
    return GoogleCalendar(connected_oauth, confirmations, audit, client=google.client())


class FakeTranscriber(Transcriber):
    """Stands in for Whisper, so tests need no model download."""

    def __init__(self, text: str = "what is on my calendar tomorrow") -> None:
        self.name = "fake-whisper"
        self.text = text
        self.calls: list[bytes] = []
        self.is_available = True

    def available(self) -> bool:
        return self.is_available

    def describe(self) -> dict:
        return {
            "provider": self.name,
            "model": "fake",
            "installed": True,
            "loaded": True,
            "on_device": True,
            "error": None,
        }

    def transcribe(self, data: bytes, *, language: str | None = None) -> Transcription:
        self.calls.append(data)
        return Transcription(
            text=self.text,
            language=language or "en",
            language_probability=0.99,
            duration_seconds=1.0,
            model="fake",
            latency_ms=5,
            segments=[{"start": 0.0, "end": 1.0, "text": self.text}],
        )


@pytest.fixture
def transcriber() -> FakeTranscriber:
    return FakeTranscriber()


@pytest.fixture
def elevenlabs_api() -> FakeElevenLabs:
    return FakeElevenLabs()


@pytest.fixture
def speech(elevenlabs_api: FakeElevenLabs, audit: AuditLog) -> ElevenLabs:
    return ElevenLabs("test-key", client=elevenlabs_api.client(), audit=audit)


@pytest.fixture
def voice(
    settings: Settings,
    transcriber: FakeTranscriber,
    speech: ElevenLabs,
    permissions: Permissions,
    confirmations: ConfirmationQueue,
    audit: AuditLog,
) -> VoiceService:
    settings.ensure_dirs()
    return VoiceService(
        transcriber=transcriber,
        speech=speech,
        permissions=permissions,
        confirmations=confirmations,
        audit=audit,
        samples_dir=settings.voice_samples_dir,
        max_audio_bytes=settings.max_audio_bytes,
        default_voice_id=None,
    )


@pytest.fixture
def assistant(
    settings: Settings,
    store: MemoryStore,
    router: ModelRouter,
    audit: AuditLog,
    permissions: Permissions,
    workspace: Workspace,
    confirmations: ConfirmationQueue,
    calendar: GoogleCalendar,
    voice: VoiceService,
) -> Assistant:
    return Assistant(
        settings=settings,
        store=store,
        router=router,
        audit=audit,
        permissions=permissions,
        workspace=workspace,
        confirmations=confirmations,
        calendar=calendar,
        voice=voice,
        owner="Ben",
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
