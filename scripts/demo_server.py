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


def main() -> None:
    settings = get_settings()
    assistant = build_assistant(settings, owner="you")
    assistant.router.cloud = ScriptedProvider("anthropic (demo)", "cloud", "claude-opus-5")
    assistant.router.local = ScriptedProvider("ollama (demo)", "local", "llama3.1:8b")
    app = create_app(settings, assistant)
    port = settings.port + 1
    print(f"\n  Billie DEMO (scripted model, no API calls)  http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
