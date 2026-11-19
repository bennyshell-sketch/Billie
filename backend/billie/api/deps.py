"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Request

from billie.assistant import Assistant
from billie.audit import AuditLog
from billie.memory.store import MemoryStore


def get_assistant(request: Request) -> Assistant:
    return request.app.state.assistant


def get_store(request: Request) -> MemoryStore:
    return request.app.state.assistant.store


def get_audit(request: Request) -> AuditLog:
    return request.app.state.assistant.audit
