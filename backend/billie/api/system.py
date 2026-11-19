"""Status, health, and the audit log."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from billie.api.deps import get_assistant
from billie.assistant import Assistant
from billie.auth import require_auth

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health() -> dict:
    """Unauthenticated liveness probe. Reveals nothing about configuration."""
    return {"status": "ok"}


@router.get("/status", dependencies=[Depends(require_auth)])
async def status(assistant: Assistant = Depends(get_assistant)) -> dict:
    return await assistant.status()


@router.get("/audit", dependencies=[Depends(require_auth)])
async def audit(
    limit: int = Query(default=100, ge=1, le=1000),
    assistant: Assistant = Depends(get_assistant),
) -> dict:
    return {"entries": assistant.audit.tail(limit)}
