"""Chat endpoints: one JSON call, one Server-Sent Events stream."""

from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from billie.api.deps import get_assistant
from billie.assistant import Assistant
from billie.auth import require_auth
from billie.models.base import ProviderError, ProviderUnavailable
from billie.schemas import ChatRequest, ChatResponse

router = APIRouter(prefix="/api", tags=["chat"], dependencies=[Depends(require_auth)])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest, assistant: Assistant = Depends(get_assistant)
) -> ChatResponse:
    try:
        result = await assistant.answer(
            payload.message,
            conversation_id=payload.conversation_id,
            route=payload.route,
            private=payload.private,
            use_memory=payload.use_memory,
            max_tokens=payload.max_tokens,
        )
    except ProviderUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return ChatResponse(**result)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest, assistant: Assistant = Depends(get_assistant)
) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        try:
            async for chunk in assistant.answer_stream(
                payload.message,
                conversation_id=payload.conversation_id,
                route=payload.route,
                private=payload.private,
                use_memory=payload.use_memory,
                max_tokens=payload.max_tokens,
            ):
                yield _sse(chunk.type, chunk.data)
        except ProviderUnavailable as exc:
            yield _sse("error", {"error": str(exc), "kind": "unavailable"})
        except ProviderError as exc:
            yield _sse("error", {"error": str(exc), "kind": "provider"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/conversations")
async def list_conversations(assistant: Assistant = Depends(get_assistant)) -> dict:
    return {"conversations": assistant.store.list_conversations()}


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str, assistant: Assistant = Depends(get_assistant)
) -> dict:
    messages = assistant.store.get_messages(conversation_id, limit=200)
    if not messages:
        raise HTTPException(status_code=404, detail="No such conversation")
    return {"conversation_id": conversation_id, "messages": messages}
