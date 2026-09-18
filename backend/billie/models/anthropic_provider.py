"""Cloud reasoning via the Anthropic Claude API.

Claude is only ever reached through this provider, using the API key from
`.env`. (Ollama hosts open-weight models only -- it cannot run Claude.)
"""

from __future__ import annotations

import time
from typing import Any, AsyncIterator

import anthropic

from billie.models.base import (
    AvailabilityCache,
    ChatMessage,
    Completion,
    LLMProvider,
    ProviderError,
    ProviderUnavailable,
    StreamEvent,
    TextDelta,
)


class AnthropicProvider(LLMProvider):
    kind = "cloud"

    def __init__(
        self,
        api_key: str | None,
        model: str = "claude-opus-5",
        max_tokens: int = 16000,
        effort: str = "high",
        cooldown_seconds: float = 60.0,
        client: Any | None = None,
    ) -> None:
        self.name = "anthropic"
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self._api_key = api_key
        self._client = client
        self._availability = AvailabilityCache(cooldown_seconds)

    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self._api_key) or self._client is not None

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self._api_key:
                raise ProviderUnavailable(
                    "No ANTHROPIC_API_KEY configured. Add one to .env to use Claude."
                )
            self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client

    def unavailable_reason(self) -> str | None:
        if not self.configured:
            return "no ANTHROPIC_API_KEY configured"
        return self._availability.reason

    async def health(self) -> bool:
        if not self.configured:
            return False
        if self._availability.in_cooldown:
            return False
        try:
            await self.client.models.retrieve(self.model)
        except Exception as exc:  # noqa: BLE001 - any failure means "not reachable"
            self._availability.mark_failed(_short_reason(exc))
            return False
        self._availability.mark_ok()
        return True

    # ------------------------------------------------------------------
    async def stream(
        self,
        system: str,
        messages: list[ChatMessage],
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if not self.configured:
            raise ProviderUnavailable(
                "No ANTHROPIC_API_KEY configured. Add one to .env to use Claude."
            )
        started = time.monotonic()
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "system": system,
            "messages": [message.to_dict() for message in messages],
        }
        if self.effort and self.effort != "high":
            # "high" is the API default; only send the field when it differs.
            request["output_config"] = {"effort": self.effort}

        try:
            async with self.client.messages.stream(**request) as stream:
                async for text in stream.text_stream:
                    if text:
                        yield TextDelta(text)
                final = await stream.get_final_message()
        except anthropic.APIConnectionError as exc:
            self._availability.mark_failed("connection error")
            raise ProviderUnavailable(f"Cannot reach the Claude API: {exc}") from exc
        except anthropic.RateLimitError as exc:
            self._availability.mark_failed("rate limited")
            raise ProviderUnavailable(f"Claude API rate limit hit: {exc}") from exc
        except anthropic.AuthenticationError as exc:
            self._availability.mark_failed("authentication failed")
            raise ProviderUnavailable(
                "Claude API rejected the API key in .env (authentication failed)."
            ) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                self._availability.mark_failed(f"server error {exc.status_code}")
                raise ProviderUnavailable(
                    f"Claude API server error ({exc.status_code})."
                ) from exc
            raise ProviderError(f"Claude API error ({exc.status_code}): {exc}") from exc

        self._availability.mark_ok()
        text = "".join(
            block.text for block in final.content if getattr(block, "type", None) == "text"
        )
        usage = final.usage.to_dict() if hasattr(final.usage, "to_dict") else {}
        yield Completion(
            text=text,
            provider=self.name,
            kind="cloud",
            model=getattr(final, "model", self.model),
            usage=usage,
            stop_reason=getattr(final, "stop_reason", None),
            latency_ms=int((time.monotonic() - started) * 1000),
        )


def _short_reason(exc: Exception) -> str:
    name = type(exc).__name__
    message = str(exc).splitlines()[0][:120] if str(exc) else ""
    return f"{name}: {message}" if message else name
