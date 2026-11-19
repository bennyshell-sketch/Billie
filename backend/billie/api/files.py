"""Permission grants, and workspace writes behind an approved diff."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from billie.api.deps import get_assistant
from billie.assistant import Assistant
from billie.auth import require_auth
from billie.permissions import FILE_READ, FILE_WRITE
from billie.schemas import (
    PermissionUpdate,
    ProposeDeleteRequest,
    ProposeWriteRequest,
    RejectRequest,
)
from billie.workspace import ProposalConflict, WorkspaceError

permissions_router = APIRouter(
    prefix="/api/permissions", tags=["permissions"], dependencies=[Depends(require_auth)]
)
workspace_router = APIRouter(
    prefix="/api/workspace", tags=["workspace"], dependencies=[Depends(require_auth)]
)


# --- permissions ----------------------------------------------------------


@permissions_router.get("")
async def list_permissions(assistant: Assistant = Depends(get_assistant)) -> dict:
    return {"permissions": assistant.permissions.state()}


@permissions_router.post("/{tool}")
async def set_permission(
    tool: str, payload: PermissionUpdate, assistant: Assistant = Depends(get_assistant)
) -> dict:
    try:
        return assistant.permissions.set(tool, payload.granted, payload.note)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No such permission: {tool}")


# --- workspace ------------------------------------------------------------


def _handle(error: WorkspaceError) -> HTTPException:
    if isinstance(error, ProposalConflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error))


@workspace_router.get("/files")
async def list_files(assistant: Assistant = Depends(get_assistant)) -> dict:
    assistant.permissions.require(FILE_READ)
    return {
        "workspace": str(assistant.workspace.root),
        "files": assistant.workspace.list_files(),
    }


@workspace_router.get("/file")
async def read_file(
    path: str = Query(min_length=1), assistant: Assistant = Depends(get_assistant)
) -> dict:
    assistant.permissions.require(FILE_READ)
    try:
        return {"path": path, "content": assistant.workspace.read(path)}
    except WorkspaceError as exc:
        raise _handle(exc)


@workspace_router.post("/propose")
async def propose_write(
    payload: ProposeWriteRequest, assistant: Assistant = Depends(get_assistant)
) -> dict:
    """Produce a diff for review. Nothing is written until it is approved."""
    assistant.permissions.require(FILE_READ)
    try:
        proposal = assistant.workspace.propose_write(payload.path, payload.content, payload.note)
    except WorkspaceError as exc:
        raise _handle(exc)
    return {
        "proposal": proposal.to_dict(),
        "requires_permission": FILE_WRITE,
        "permission_granted": assistant.permissions.is_granted(FILE_WRITE),
    }


@workspace_router.post("/propose-delete")
async def propose_delete(
    payload: ProposeDeleteRequest, assistant: Assistant = Depends(get_assistant)
) -> dict:
    assistant.permissions.require(FILE_READ)
    try:
        proposal = assistant.workspace.propose_delete(payload.path, payload.note)
    except WorkspaceError as exc:
        raise _handle(exc)
    return {"proposal": proposal.to_dict()}


@workspace_router.get("/proposals")
async def list_proposals(
    all: bool = Query(default=False, description="Include resolved proposals"),
    assistant: Assistant = Depends(get_assistant),
) -> dict:
    proposals = assistant.workspace.history() if all else assistant.workspace.pending()
    return {"proposals": [proposal.to_dict() for proposal in proposals]}


@workspace_router.get("/proposals/{proposal_id}")
async def get_proposal(
    proposal_id: str, assistant: Assistant = Depends(get_assistant)
) -> dict:
    try:
        return assistant.workspace.get(proposal_id).to_dict(include_content=True)
    except WorkspaceError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@workspace_router.post("/proposals/{proposal_id}/approve")
async def approve(proposal_id: str, assistant: Assistant = Depends(get_assistant)) -> dict:
    """The only endpoint in Billie that writes to your disk."""
    assistant.permissions.require(FILE_WRITE)
    try:
        result = assistant.workspace.approve(proposal_id)
    except WorkspaceError as exc:
        raise _handle(exc)
    # Keep memory in step with what is now on disk.
    if assistant.permissions.is_granted(FILE_READ):
        assistant.reindex_files()
    return result


@workspace_router.post("/proposals/{proposal_id}/reject")
async def reject(
    proposal_id: str,
    payload: RejectRequest | None = None,
    assistant: Assistant = Depends(get_assistant),
) -> dict:
    try:
        return assistant.workspace.reject(proposal_id, (payload.reason if payload else ""))
    except WorkspaceError as exc:
        raise _handle(exc)
