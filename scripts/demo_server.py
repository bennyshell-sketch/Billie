#!/usr/bin/env python3
"""Run Billie's UI with a scripted stand-in model.

Useful for working on the interface before you have an API key or Ollama
installed: everything is real except the model itself, which replies with a
canned answer that cites whatever was retrieved from your notes.

    python scripts/demo_server.py          # http://127.0.0.1:8766

Never use this as your actual assistant -- it does not think.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import AsyncIterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn  # noqa: E402

from billie.app import build_assistant, create_app  # noqa: E402
from billie.config import get_settings  # noqa: E402
from billie.integrations.google_calendar import GoogleCalendar  # noqa: E402
from billie.integrations.google_oauth import GoogleOAuth  # noqa: E402
from billie.integrations.token_store import TokenStore  # noqa: E402
from billie.voice.speech import ElevenLabs  # noqa: E402
from billie.voice.transcribe import Transcription  # noqa: E402
from billie.models.base import (  # noqa: E402
    ChatMessage,
    Completion,
    LLMProvider,
    StreamEvent,
    TextDelta,
)


class ScriptedProvider(LLMProvider):
    def __init__(self, name: str, kind: str, model: str) -> None:
        self.name = name
        self.kind = kind  # type: ignore[assignment]
        self.model = model

    def unavailable_reason(self) -> str | None:
        return None

    async def health(self) -> bool:
        return True

    async def stream(
        self, system: str, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> AsyncIterator[StreamEvent]:
        question = messages[-1].content
        cited = "[S1]" if "[S1]" in question else ""
        answer = (
            f"This is the {self.kind} stand-in model ({self.model}) replying. "
            f"{'I used the retrieved note ' + cited + '.' if cited else 'Nothing was retrieved, so I have no source for this.'} "
            "Swap in a real model by adding ANTHROPIC_API_KEY to .env or starting Ollama."
        )
        for word in answer.split(" "):
            await asyncio.sleep(0.02)
            yield TextDelta(word + " ")
        yield Completion(
            text=answer,
            provider=self.name,
            kind=self.kind,  # type: ignore[arg-type]
            model=self.model,
            usage={"input_tokens": len(question.split()), "output_tokens": len(answer.split())},
            stop_reason="end_turn",
            latency_ms=400,
        )


def fake_calendar(assistant) -> None:
    """Wire the calendar to an in-memory Google, so the panel is demoable."""
    sys.path.insert(0, str(ROOT))
    from tests.fake_google import FakeGoogle

    google = FakeGoogle()
    google.add_event(
        id="evt_standup",
        summary="Team standup",
        start={"dateTime": "2027-03-14T09:00:00+00:00"},
        end={"dateTime": "2027-03-14T09:15:00+00:00"},
        location="Zoom",
    )
    google.add_event(
        id="evt_grant",
        summary="Grant deadline",
        start={"date": "2027-03-19"},
        end={"date": "2027-03-20"},
    )
    store = TokenStore(fallback_path=ROOT / "data" / "demo-credentials.json", allow_file=True)
    oauth = GoogleOAuth(
        "demo-client-id", "demo-client-secret", store, client=google.client(), audit=assistant.audit
    )
    oauth.token_store.save(
        oauth.account,
        {
            "refresh_token": "demo-refresh-token",
            "access_token": "demo-access-token",
            "expires_at": 4102444800.0,
            "scopes": list(oauth.scopes),
            "email": "you@example.com (demo)",
            "connected_at": "2027-03-01T00:00:00Z",
        },
    )
    assistant.calendar = GoogleCalendar(
        oauth, assistant.confirmations, assistant.audit, client=google.client()
    )


def fake_voice(assistant) -> None:
    """Stand-in speech: a canned transcript and an in-memory ElevenLabs."""
    sys.path.insert(0, str(ROOT))
    from tests.fake_elevenlabs import FakeElevenLabs

    class ScriptedTranscriber:
        name = "demo-whisper"

        def available(self):
            return True

        def describe(self):
            return {
                "provider": self.name,
                "model": "demo",
                "installed": True,
                "loaded": True,
                "on_device": True,
                "error": None,
            }

        def transcribe(self, data: bytes, *, language: str | None = None) -> Transcription:
            return Transcription(
                text="What have I got on this week?",
                language="en",
                language_probability=0.99,
                duration_seconds=round(len(data) / 32000, 2),
                model="demo",
                latency_ms=40,
            )

    assistant.voice.transcriber = ScriptedTranscriber()
    assistant.voice.speech = ElevenLabs(
        "demo-key", client=FakeElevenLabs().client(), audit=assistant.audit
    )


def main() -> None:
    settings = get_settings()
    assistant = build_assistant(settings, owner="you")
    assistant.router.cloud = ScriptedProvider("anthropic (demo)", "cloud", "claude-opus-5")
    assistant.router.local = ScriptedProvider("ollama (demo)", "local", "llama3.1:8b")
    fake_calendar(assistant)
    fake_voice(assistant)
    app = create_app(settings, assistant)
    port = settings.port + 1
    print(f"\n  Billie DEMO (scripted model, calendar and voice; no API calls)")
    print(f"  http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
