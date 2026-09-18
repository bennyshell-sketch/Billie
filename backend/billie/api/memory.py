"""Memory endpoints: index the notes folder, teach facts, search."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from billie.api.deps import get_assistant
from billie.assistant import Assistant
from billie.auth import require_auth
from billie.schemas import SearchRequest, TeachRequest

router = APIRouter(prefix="/api/memory", tags=["memory"], dependencies=[Depends(require_auth)])


@router.post("/reindex")
async def reindex(assistant: Assistant = Depends(get_assistant)) -> dict:
    """Re-index every allow-listed folder."""
    return assistant.reindex_files().to_dict()


@router.post("/teach")
async def teach(payload: TeachRequest, assistant: Assistant = Depends(get_assistant)) -> dict:
    return assistant.teach(payload.content, title=payload.title, tags=payload.tags)


@router.post("/search")
async def search(payload: SearchRequest, assistant: Assistant = Depends(get_assistant)) -> dict:
    results = assistant.search(payload.query, top_k=payload.top_k)
    return {"query": payload.query, "results": [chunk.to_dict() for chunk in results]}


@router.get("/documents")
async def documents(assistant: Assistant = Depends(get_assistant)) -> dict:
    return {"documents": assistant.store.list_documents()}
