"""Request and response models for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = None
    #: "auto" lets the router decide; "local"/"cloud" force a provider.
    route: Literal["auto", "local", "cloud"] | None = None
    #: Never leaves this machine. Local model only, no cloud fallback.
    private: bool = False
    use_memory: bool = True
    max_tokens: int | None = Field(default=None, ge=1, le=128_000)


class ChatResponse(BaseModel):
    conversation_id: str
    message_id: str
    answer: str
    routing: dict[str, Any]
    grounding: dict[str, Any]
    sources: list[dict[str, Any]]
    usage: dict[str, Any] = {}
    latency_ms: int | None = None


class TeachRequest(BaseModel):
    """Something you are telling Billie to remember."""

    content: str = Field(min_length=1)
    title: str | None = None
    tags: list[str] = []


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)


class StatusResponse(BaseModel):
    version: str
    providers: dict[str, Any]
    memory: dict[str, Any]
    routing: dict[str, Any]
    auth: dict[str, Any]
