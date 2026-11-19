"""The routing layer.

The privacy guarantee -- a request flagged private is never sent to the cloud,
under any failure mode -- is the most important behaviour in this file.
"""

from __future__ import annotations

import pytest

from billie.models.base import ChatMessage, Completion, ProviderUnavailable
from billie.models.router import ModelRouter, score_complexity
from tests.conftest import FakeProvider


def build_router(cloud: FakeProvider, local: FakeProvider, **kwargs) -> ModelRouter:
    return ModelRouter(cloud=cloud, local=local, **kwargs)


# --- complexity heuristic -------------------------------------------------


@pytest.mark.parametrize("prompt", ["hi", "thanks", "ok", "hello there"])
def test_trivial_prompts_score_low(prompt):
    assert score_complexity(prompt) < 0.45


@pytest.mark.parametrize(
    "prompt",
    [
        "Analyse the trade-offs between SQLite and Postgres for this app and recommend one.",
        "Refactor this function and explain why the new structure is better.",
        "Write me a step-by-step plan for learning statistics over six months.",
    ],
)
def test_substantial_prompts_score_high(prompt):
    assert score_complexity(prompt) >= 0.45


def test_retrieved_context_and_history_raise_the_score():
    prompt = "What did I say about the deadline?"
    assert score_complexity(prompt, history_length=10, context_chunks=6) > score_complexity(prompt)


# --- routing decisions ----------------------------------------------------


async def test_private_requests_always_go_local_even_when_cloud_is_healthy(cloud, local):
    router = build_router(cloud, local)
    decision = await router.decide(
        "Analyse my full medical history in detail and explain the trade-offs.", private=True
    )
    assert decision.kind == "local"
    assert decision.private is True
    assert "private" in decision.reason


async def test_private_request_errors_rather_than_using_the_cloud(cloud, local):
    local.healthy = False
    router = build_router(cloud, local)
    with pytest.raises(ProviderUnavailable) as excinfo:
        await router.decide("Something sensitive", private=True)
    assert "private" in str(excinfo.value).lower()


async def test_private_request_never_falls_back_to_cloud_mid_stream(cloud, local):
    local.fail_with = ProviderUnavailable("ollama died")
    router = build_router(cloud, local)
    decision = await router.decide("Something sensitive", private=True)
    with pytest.raises(ProviderUnavailable):
        async for _ in router.stream(decision, "system", [ChatMessage("user", "hi")]):
            pass
    assert cloud.calls == []


async def test_offline_cloud_routes_to_local(cloud, local):
    cloud.healthy = False
    router = build_router(cloud, local)
    decision = await router.decide(
        "Analyse the trade-offs between these two designs and recommend one."
    )
    assert decision.kind == "local"
    assert "unavailable" in decision.reason


async def test_missing_local_routes_to_cloud(cloud, local):
    local.healthy = False
    router = build_router(cloud, local)
    decision = await router.decide("hi")
    assert decision.kind == "cloud"


async def test_complexity_decides_when_both_are_available(cloud, local):
    router = build_router(cloud, local, complexity_threshold=0.45)
    simple = await router.decide("hi")
    complex_ask = await router.decide(
        "Analyse the trade-offs between SQLite and Postgres and recommend one."
    )
    assert simple.kind == "local"
    assert complex_ask.kind == "cloud"


async def test_explicit_route_overrides_complexity(cloud, local):
    router = build_router(cloud, local)
    decision = await router.decide("hi", route="cloud")
    assert decision.kind == "cloud" and decision.forced is True
    decision = await router.decide(
        "Analyse everything in depth and compare the options.", route="local"
    )
    assert decision.kind == "local" and decision.forced is True


async def test_forced_route_to_an_unavailable_provider_errors(cloud, local):
    cloud.healthy = False
    router = build_router(cloud, local)
    with pytest.raises(ProviderUnavailable):
        await router.decide("hi", route="cloud")


async def test_no_provider_at_all_errors_clearly(cloud, local):
    cloud.healthy = False
    local.healthy = False
    router = build_router(cloud, local)
    with pytest.raises(ProviderUnavailable) as excinfo:
        await router.decide("hi")
    assert "No model available" in str(excinfo.value)


# --- streaming and fallback ----------------------------------------------


async def test_stream_yields_deltas_then_one_completion(cloud, local):
    router = build_router(cloud, local)
    decision = await router.decide("hi")
    events = [
        event async for event in router.stream(decision, "sys", [ChatMessage("user", "hi")])
    ]
    assert isinstance(events[-1], Completion)
    assert sum(isinstance(event, Completion) for event in events) == 1
    assert "".join(event.text for event in events[:-1]).strip() == "Local answer."


async def test_a_dead_provider_falls_back_once(cloud, local):
    local.fail_with = ProviderUnavailable("ollama is down")
    router = build_router(cloud, local)
    decision = await router.decide("hi")
    assert decision.kind == "local"

    events = [
        event async for event in router.stream(decision, "sys", [ChatMessage("user", "hi")])
    ]
    final = events[-1]
    assert isinstance(final, Completion)
    assert final.provider == "anthropic"
    assert decision.fell_back_from == "ollama"
    assert decision.kind == "cloud"


async def test_a_forced_route_does_not_silently_fall_back(cloud, local):
    local.fail_with = ProviderUnavailable("ollama is down")
    router = build_router(cloud, local)
    decision = await router.decide("hi", route="local")
    with pytest.raises(ProviderUnavailable):
        async for _ in router.stream(decision, "sys", [ChatMessage("user", "hi")]):
            pass
    assert cloud.calls == []


async def test_availability_report_lists_both_providers(cloud, local):
    local.healthy = False
    router = build_router(cloud, local)
    availability = await router.availability()
    assert availability["cloud"]["available"] is True
    assert availability["local"]["available"] is False
    assert availability["local"]["reason"]
