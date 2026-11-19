"""Wire-protocol behaviour of the two real providers.

The rest of the suite uses fake providers. These tests exercise the actual
parsing and error mapping in `OllamaProvider` and `AnthropicProvider` without
touching the network: Ollama is served by an httpx mock transport, and the
Anthropic client is a stand-in shaped like the SDK's streaming interface.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx
import httpx2
import pytest

from billie.models.anthropic_provider import AnthropicProvider
from billie.models.base import (
    ChatMessage,
    Completion,
    ProviderError,
    ProviderUnavailable,
    TextDelta,
)
from billie.models.ollama_provider import OllamaProvider

MESSAGES = [ChatMessage("user", "hello")]


# --- Ollama ---------------------------------------------------------------


def ollama_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ndjson(*events: dict) -> bytes:
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


async def test_ollama_parses_a_streamed_ndjson_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        assert body["stream"] is True
        # The system prompt must be the first message Ollama sees.
        assert body["messages"][0]["role"] == "system"
        return httpx.Response(
            200,
            content=ndjson(
                {"message": {"content": "Local "}, "done": False},
                {"message": {"content": "answer."}, "done": False},
                {
                    "done": True,
                    "model": "llama3.1:8b",
                    "done_reason": "stop",
                    "prompt_eval_count": 31,
                    "eval_count": 7,
                },
            ),
        )

    provider = OllamaProvider(client=ollama_client(handler))
    events = [event async for event in provider.stream("system prompt", MESSAGES)]
    assert [event.text for event in events if isinstance(event, TextDelta)] == [
        "Local ",
        "answer.",
    ]
    final = events[-1]
    assert isinstance(final, Completion)
    assert final.text == "Local answer."
    assert final.kind == "local"
    assert final.usage == {"input_tokens": 31, "output_tokens": 7}
    assert final.stop_reason == "stop"


async def test_ollama_reports_a_missing_model_with_the_pull_command():
    provider = OllamaProvider(
        model="qwen2.5:14b",
        client=ollama_client(lambda request: httpx.Response(404, json={"error": "model not found"})),
    )
    with pytest.raises(ProviderUnavailable) as excinfo:
        async for _ in provider.stream("system", MESSAGES):
            pass
    assert "ollama pull qwen2.5:14b" in str(excinfo.value)


async def test_ollama_surfaces_an_inline_error_event():
    provider = OllamaProvider(
        client=ollama_client(
            lambda request: httpx.Response(200, content=ndjson({"error": "out of memory"}))
        )
    )
    with pytest.raises(ProviderError) as excinfo:
        async for _ in provider.stream("system", MESSAGES):
            pass
    assert "out of memory" in str(excinfo.value)


async def test_ollama_connection_failure_is_unavailable_not_a_crash():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = OllamaProvider(client=ollama_client(refuse))
    with pytest.raises(ProviderUnavailable) as excinfo:
        async for _ in provider.stream("system", MESSAGES):
            pass
    assert "ollama serve" in str(excinfo.value)
    # The failure is remembered, so the next request fails fast instead of
    # waiting on another timeout.
    assert await provider.health() is False


async def test_ollama_health_reads_the_tags_endpoint():
    provider = OllamaProvider(
        client=ollama_client(
            lambda request: httpx.Response(200, json={"models": [{"name": "llama3.1:8b"}]})
        )
    )
    assert await provider.health() is True
    assert await provider.installed_models() == ["llama3.1:8b"]


# --- Anthropic ------------------------------------------------------------


class FakeStream:
    def __init__(self, pieces: list[str], final) -> None:
        self.pieces = pieces
        self.final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    @property
    def text_stream(self):
        async def generator():
            for piece in self.pieces:
                yield piece

        return generator()

    async def get_final_message(self):
        return self.final


class FakeAnthropicClient:
    """Shaped like `anthropic.AsyncAnthropic` for the one call Billie makes."""

    def __init__(self, pieces: list[str] | None = None, raises: Exception | None = None) -> None:
        self.pieces = pieces or ["Cloud ", "answer."]
        self.raises = raises
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.requests.append(kwargs)
        if self.raises is not None:
            raise self.raises
        final = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="".join(self.pieces))],
            usage=SimpleNamespace(to_dict=lambda: {"input_tokens": 120, "output_tokens": 9}),
            model="claude-opus-5",
            stop_reason="end_turn",
        )
        return FakeStream(self.pieces, final)


def http_response(status: int) -> httpx2.Response:
    return httpx2.Response(
        status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )


async def test_anthropic_streams_text_and_reports_usage():
    client = FakeAnthropicClient()
    provider = AnthropicProvider(api_key=None, client=client)
    events = [event async for event in provider.stream("system prompt", MESSAGES)]
    assert [event.text for event in events if isinstance(event, TextDelta)] == ["Cloud ", "answer."]
    final = events[-1]
    assert isinstance(final, Completion)
    assert final.text == "Cloud answer."
    assert final.kind == "cloud"
    assert final.model == "claude-opus-5"
    assert final.usage == {"input_tokens": 120, "output_tokens": 9}

    request = client.requests[0]
    assert request["model"] == "claude-opus-5"
    assert request["system"] == "system prompt"
    assert request["messages"] == [{"role": "user", "content": "hello"}]
    # "high" is the API default, so no output_config is sent for it.
    assert "output_config" not in request


async def test_anthropic_sends_effort_only_when_it_differs_from_the_default():
    client = FakeAnthropicClient()
    provider = AnthropicProvider(api_key=None, model="claude-opus-5", effort="low", client=client)
    async for _ in provider.stream("system", MESSAGES):
        pass
    assert client.requests[0]["output_config"] == {"effort": "low"}


async def test_a_missing_api_key_is_a_clear_unavailable_error():
    provider = AnthropicProvider(api_key=None)
    assert provider.configured is False
    assert provider.unavailable_reason() == "no ANTHROPIC_API_KEY configured"
    assert await provider.health() is False
    with pytest.raises(ProviderUnavailable) as excinfo:
        async for _ in provider.stream("system", MESSAGES):
            pass
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APIConnectionError(
            request=httpx2.Request("POST", "https://api.anthropic.com")
        ),
        anthropic.RateLimitError("slow down", response=http_response(429), body=None),
        anthropic.AuthenticationError("bad key", response=http_response(401), body=None),
        anthropic.InternalServerError("boom", response=http_response(500), body=None),
    ],
)
async def test_retryable_api_failures_become_provider_unavailable(error):
    """Unavailable is the signal the router uses to fall back to the local model."""
    provider = AnthropicProvider(api_key=None, client=FakeAnthropicClient(raises=error))
    with pytest.raises(ProviderUnavailable):
        async for _ in provider.stream("system", MESSAGES):
            pass
    assert provider.unavailable_reason() is not None


async def test_a_bad_request_is_an_error_not_a_fallback():
    """A 400 is Billie's own bug; retrying on the local model would hide it."""
    error = anthropic.BadRequestError("bad params", response=http_response(400), body=None)
    provider = AnthropicProvider(api_key=None, client=FakeAnthropicClient(raises=error))
    with pytest.raises(ProviderError) as excinfo:
        async for _ in provider.stream("system", MESSAGES):
            pass
    assert not isinstance(excinfo.value, ProviderUnavailable)
