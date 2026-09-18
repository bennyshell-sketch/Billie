"""The routing layer: local model or Claude, decided per request.

Three inputs decide, in this order of authority:

1. **Privacy.** A request flagged private is answered locally or not at all.
   There is no fallback to the cloud for private requests -- that would defeat
   the point of the flag.
2. **Availability.** No API key, or the Claude API unreachable (offline), means
   local. Ollama not running means cloud.
3. **Complexity.** A heuristic score over the request decides the rest: short,
   simple requests stay local (fast, free, private); longer or analytical ones
   go to Claude.

An explicit per-request route ("local" / "cloud") overrides 3, and overrides 2
only in the sense that it produces a clear error instead of a silent switch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

from billie.models.base import (
    ChatMessage,
    Completion,
    LLMProvider,
    ProviderUnavailable,
    StreamEvent,
)

RouteChoice = Literal["auto", "local", "cloud"]
Kind = Literal["local", "cloud"]

# Words that suggest the request wants real reasoning rather than a lookup.
_COMPLEX_MARKERS = re.compile(
    r"\b(analys|analyz|compare|critique|design|refactor|debug|plan|strategy|"
    r"implement|architect|research|summaris|summariz|evaluat|trade-?off|"
    r"why\s+does|why\s+is|how\s+would|step[-\s]by[-\s]step|pros\s+and\s+cons|"
    r"write\s+(me\s+)?(a|an|the)\s+\w+|draft|essay|proof|derive|optimis|optimiz)",
    re.IGNORECASE,
)
_SIMPLE_MARKERS = re.compile(
    r"^\s*(hi|hey|hello|thanks|thank you|yes|no|ok|okay|cheers|"
    r"what time|what'?s the time|remind me|note that|remember that)\b",
    re.IGNORECASE,
)
_CODE_FENCE = re.compile(r"```")


@dataclass
class RoutingDecision:
    kind: Kind
    provider: str
    model: str
    reason: str
    complexity: float
    forced: bool = False
    private: bool = False
    fell_back_from: str | None = None
    considered: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "provider": self.provider,
            "model": self.model,
            "reason": self.reason,
            "complexity": round(self.complexity, 3),
            "forced": self.forced,
            "private": self.private,
            "fell_back_from": self.fell_back_from,
            "considered": self.considered,
        }


def score_complexity(
    prompt: str,
    *,
    history_length: int = 0,
    context_chunks: int = 0,
) -> float:
    """Rough 0.0-1.0 estimate of how much reasoning a request needs.

    Deliberately a transparent heuristic rather than a model call: routing must
    not itself require the model it is choosing between.
    """
    text = prompt.strip()
    if not text:
        return 0.0

    score = 0.0
    words = len(text.split())
    score += min(words / 80.0, 0.35)            # long asks tend to be harder
    if _COMPLEX_MARKERS.search(text):
        score += 0.40
    if _CODE_FENCE.search(text) or "def " in text or "function " in text:
        score += 0.15
    if text.count("?") > 1:
        score += 0.05
    score += min(history_length / 20.0, 0.10)   # deep threads need more context
    score += min(context_chunks / 10.0, 0.10)   # retrieved context to weigh up

    if _SIMPLE_MARKERS.search(text) and words < 12:
        score -= 0.45
    if words <= 4:
        score -= 0.15

    return max(0.0, min(1.0, score))


class ModelRouter:
    """Chooses a provider per request and streams from it."""

    def __init__(
        self,
        *,
        cloud: LLMProvider,
        local: LLMProvider,
        default_route: RouteChoice = "auto",
        complexity_threshold: float = 0.45,
        audit=None,
    ) -> None:
        self.cloud = cloud
        self.local = local
        self.default_route = default_route
        self.complexity_threshold = complexity_threshold
        self.audit = audit

    # ------------------------------------------------------------------
    async def availability(self) -> dict[str, dict[str, Any]]:
        cloud_ok = await self.cloud.health()
        local_ok = await self.local.health()
        return {
            "cloud": {
                "provider": self.cloud.name,
                "model": self.cloud.model,
                "available": cloud_ok,
                "reason": getattr(self.cloud, "unavailable_reason", lambda: None)(),
            },
            "local": {
                "provider": self.local.name,
                "model": self.local.model,
                "available": local_ok,
                "reason": getattr(self.local, "unavailable_reason", lambda: None)(),
            },
        }

    async def decide(
        self,
        prompt: str,
        *,
        route: RouteChoice | None = None,
        private: bool = False,
        history_length: int = 0,
        context_chunks: int = 0,
    ) -> RoutingDecision:
        route = route or self.default_route
        complexity = score_complexity(
            prompt, history_length=history_length, context_chunks=context_chunks
        )

        # 1. Privacy wins over everything.
        if private:
            local_ok = await self.local.health()
            if not local_ok:
                reason = (
                    getattr(self.local, "unavailable_reason", lambda: None)()
                    or "not reachable"
                )
                raise ProviderUnavailable(
                    "This request is flagged private, so it can only be answered by the "
                    f"local model -- but {self.local.name} is unavailable ({reason}). "
                    "Start Ollama, or un-flag the request to allow the Claude API."
                )
            return self._local_decision(
                "flagged private: answered on-device only",
                complexity,
                forced=route == "local",
                private=True,
            )

        # 2. An explicit route is honoured, with a clear error if impossible.
        if route == "local":
            if not await self.local.health():
                reason = (
                    getattr(self.local, "unavailable_reason", lambda: None)()
                    or "not reachable"
                )
                raise ProviderUnavailable(
                    f"Local model requested but {self.local.name} is unavailable ({reason})."
                )
            return self._local_decision("local model requested", complexity, forced=True)

        if route == "cloud":
            if not await self.cloud.health():
                reason = (
                    getattr(self.cloud, "unavailable_reason", lambda: None)()
                    or "not reachable"
                )
                raise ProviderUnavailable(
                    f"Claude API requested but unavailable ({reason})."
                )
            return self._cloud_decision("Claude API requested", complexity, forced=True)

        # 3. Auto: availability, then complexity.
        cloud_ok = await self.cloud.health()
        local_ok = await self.local.health()
        considered = {"cloud_available": cloud_ok, "local_available": local_ok}

        if not cloud_ok and not local_ok:
            cloud_reason = (
                getattr(self.cloud, "unavailable_reason", lambda: None)() or "unreachable"
            )
            local_reason = (
                getattr(self.local, "unavailable_reason", lambda: None)() or "unreachable"
            )
            raise ProviderUnavailable(
                f"No model available. Claude API: {cloud_reason}. "
                f"Local ({self.local.name}): {local_reason}."
            )
        if not cloud_ok:
            reason = (
                getattr(self.cloud, "unavailable_reason", lambda: None)() or "unreachable"
            )
            return self._local_decision(
                f"Claude API unavailable ({reason}) -- answered on-device",
                complexity,
                considered=considered,
            )
        if not local_ok:
            return self._cloud_decision(
                "local model unavailable -- answered by Claude",
                complexity,
                considered=considered,
            )

        if complexity >= self.complexity_threshold:
            return self._cloud_decision(
                f"complexity {complexity:.2f} >= threshold {self.complexity_threshold:.2f}",
                complexity,
                considered=considered,
            )
        return self._local_decision(
            f"complexity {complexity:.2f} < threshold {self.complexity_threshold:.2f}",
            complexity,
            considered=considered,
        )

    def _local_decision(self, reason: str, complexity: float, **kwargs) -> RoutingDecision:
        return RoutingDecision(
            kind="local",
            provider=self.local.name,
            model=self.local.model,
            reason=reason,
            complexity=complexity,
            **kwargs,
        )

    def _cloud_decision(self, reason: str, complexity: float, **kwargs) -> RoutingDecision:
        return RoutingDecision(
            kind="cloud",
            provider=self.cloud.name,
            model=self.cloud.model,
            reason=reason,
            complexity=complexity,
            **kwargs,
        )

    # ------------------------------------------------------------------
    def provider_for(self, decision: RoutingDecision) -> LLMProvider:
        return self.local if decision.kind == "local" else self.cloud

    async def stream(
        self,
        decision: RoutingDecision,
        system: str,
        messages: list[ChatMessage],
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream from the chosen provider, falling back once if it dies.

        A private request never falls back to the cloud, and a request that has
        already emitted text is not restarted on the other provider (that would
        duplicate half an answer) -- the error is raised instead.
        """
        provider = self.provider_for(decision)
        emitted = False
        try:
            async for event in provider.stream(system, messages, max_tokens=max_tokens):
                if isinstance(event, Completion):
                    yield self._tag(event, decision)
                else:
                    emitted = True
                    yield event
            return
        except ProviderUnavailable as exc:
            if emitted or decision.private or decision.forced:
                raise
            fallback_kind: Kind = "cloud" if decision.kind == "local" else "local"
            fallback = self.cloud if fallback_kind == "cloud" else self.local
            if not await fallback.health():
                raise
            if self.audit:
                self.audit.record(
                    "model.fallback",
                    from_provider=provider.name,
                    to_provider=fallback.name,
                    error=str(exc),
                )
            decision.fell_back_from = provider.name
            decision.kind = fallback_kind
            decision.provider = fallback.name
            decision.model = fallback.model
            decision.reason = f"{provider.name} failed mid-request ({exc}); used {fallback.name}"

        async for event in fallback.stream(system, messages, max_tokens=max_tokens):
            if isinstance(event, Completion):
                yield self._tag(event, decision)
            else:
                yield event

    @staticmethod
    def _tag(completion: Completion, decision: RoutingDecision) -> Completion:
        decision.model = completion.model
        return completion
