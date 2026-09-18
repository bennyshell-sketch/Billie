"""Provider-agnostic chat interface.

Both the cloud provider (Anthropic) and the local provider (Ollama) implement
`LLMProvider`, so the router can treat them identically and tests can swap in
a fake.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

Role = Literal["user", "assistant"]
Kind = Literal["local", "cloud"]


@dataclass
class ChatMessage:
    role: Role
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class TextDelta:
    """An incremental piece of the answer."""

    text: str


@dataclass
class Completion:
    """The final event of a stream: the whole answer plus metadata."""

    text: str
    provider: str
    kind: Kind
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None
    latency_ms: int | None = None


StreamEvent = TextDelta | Completion


class ProviderError(RuntimeError):
    """The provider failed in a way the caller should surface."""


class ProviderUnavailable(ProviderError):
    """The provider cannot be reached right now (offline, down, not configured).

    The router treats this as a reason to fall back to the other provider --
    except when the request was flagged private, where falling back to the
    cloud is never acceptable.
    """


class LLMProvider(ABC):
    """A chat model Billie can talk to."""

    name: str
    kind: Kind
    model: str

    @abstractmethod
    async def stream(
        self,
        system: str,
        messages: list[ChatMessage],
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Yield `TextDelta`s, then exactly one final `Completion`."""
        raise NotImplementedError
        yield  # pragma: no cover - makes this an async generator for typing

    @abstractmethod
    async def health(self) -> bool:
        """Cheap reachability probe."""

    async def complete(
        self,
        system: str,
        messages: list[ChatMessage],
        *,
        max_tokens: int | None = None,
    ) -> Completion:
        """Non-streaming convenience wrapper around `stream`."""
        final: Completion | None = None
        async for event in self.stream(system, messages, max_tokens=max_tokens):
            if isinstance(event, Completion):
                final = event
        if final is None:
            raise ProviderError(f"{self.name} produced no completion")
        return final


class AvailabilityCache:
    """Remembers a provider failure for a cooldown window.

    Without this, every request to an offline provider would pay the full
    connection timeout before falling back.
    """

    def __init__(self, cooldown_seconds: float = 60.0) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._failed_at: float | None = None
        self._reason: str | None = None

    def mark_failed(self, reason: str) -> None:
        self._failed_at = time.monotonic()
        self._reason = reason

    def mark_ok(self) -> None:
        self._failed_at = None
        self._reason = None

    @property
    def reason(self) -> str | None:
        return self._reason if self.in_cooldown else None

    @property
    def in_cooldown(self) -> bool:
        if self._failed_at is None:
            return False
        if time.monotonic() - self._failed_at >= self.cooldown_seconds:
            self._failed_at = None
            self._reason = None
            return False
        return True
