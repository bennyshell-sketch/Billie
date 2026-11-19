"""Local, offline model via Ollama.

Ollama serves open-weight models (Llama, Qwen, ...) from this machine. Nothing
sent to this provider leaves the device, which is what makes it the correct
destination for anything flagged private.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

import httpx

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


class OllamaProvider(LLMProvider):
    kind = "local"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "llama3.1:8b",
        timeout: float = 300.0,
        cooldown_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = "ollama"
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._client = client
        self._availability = AvailabilityCache(cooldown_seconds)

    @property
    def configured(self) -> bool:
        return True  # Ollama needs no key; reachability is the only question.

    def unavailable_reason(self) -> str | None:
        return self._availability.reason

    def _http(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(timeout=self.timeout)

    async def health(self) -> bool:
        if self._availability.in_cooldown:
            return False
        client = self._http()
        try:
            response = await client.get(f"{self.base_url}/api/tags", timeout=3.0)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            self._availability.mark_failed(f"{type(exc).__name__}")
            return False
        finally:
            if self._client is None:
                await client.aclose()
        self._availability.mark_ok()
        return True

    async def installed_models(self) -> list[str]:
        client = self._http()
        try:
            response = await client.get(f"{self.base_url}/api/tags", timeout=3.0)
            response.raise_for_status()
            return [model["name"] for model in response.json().get("models", [])]
        except Exception:  # noqa: BLE001
            return []
        finally:
            if self._client is None:
                await client.aclose()

    async def stream(
        self,
        system: str,
        messages: list[ChatMessage],
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        started = time.monotonic()
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": True,
            "messages": [{"role": "system", "content": system}]
            + [message.to_dict() for message in messages],
        }
        if max_tokens:
            payload["options"] = {"num_predict": max_tokens}

        client = self._http()
        chunks: list[str] = []
        final_payload: dict[str, Any] = {}
        try:
            async with client.stream(
                "POST", f"{self.base_url}/api/chat", json=payload, timeout=self.timeout
            ) as response:
                if response.status_code == 404:
                    await response.aread()
                    raise ProviderUnavailable(
                        f"Ollama has no model named {self.model!r}. "
                        f"Install it with: ollama pull {self.model}"
                    )
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("error"):
                        raise ProviderError(f"Ollama error: {event['error']}")
                    piece = (event.get("message") or {}).get("content", "")
                    if piece:
                        chunks.append(piece)
                        yield TextDelta(piece)
                    if event.get("done"):
                        final_payload = event
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            self._availability.mark_failed(type(exc).__name__)
            raise ProviderUnavailable(
                f"Cannot reach Ollama at {self.base_url}. Is `ollama serve` running?"
            ) from exc
        except httpx.HTTPStatusError as exc:
            self._availability.mark_failed(f"HTTP {exc.response.status_code}")
            raise ProviderUnavailable(f"Ollama returned HTTP {exc.response.status_code}") from exc
        finally:
            if self._client is None:
                await client.aclose()

        self._availability.mark_ok()
        yield Completion(
            text="".join(chunks),
            provider=self.name,
            kind="local",
            model=final_payload.get("model", self.model),
            usage={
                "input_tokens": final_payload.get("prompt_eval_count"),
                "output_tokens": final_payload.get("eval_count"),
            },
            stop_reason=final_payload.get("done_reason"),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
