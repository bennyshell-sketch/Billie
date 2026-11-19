"""Google Calendar endpoints.

Reads happen directly. Every write is proposed first and waits for an explicit
approval, so nothing reaches Google without you seeing exactly what it will do.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from billie.api.deps import get_assistant
from billie.assistant import Assistant
from billie.auth import require_auth
from billie.confirmations import ConfirmationError
from billie.integrations.google_calendar import CalendarError
from billie.integrations.google_oauth import OAuthError
from billie.integrations.token_store import TokenStoreUnavailable
from billie.permissions import CALENDAR_READ, CALENDAR_WRITE
from billie.schemas import (
    CreateEventRequest,
    MoveEventRequest,
    RejectRequest,
)

router = APIRouter(prefix="/api/calendar", tags=["calendar"], dependencies=[Depends(require_auth)])

#: The OAuth callback is hit by the browser coming back from Google, which
#: cannot present the API token, so it is registered without the auth
#: dependency and protected by the `state` parameter instead.
callback_router = APIRouter(prefix="/api/calendar", tags=["calendar"])

CALLBACK_PATH = "/api/calendar/oauth/callback"


def _calendar(assistant: Assistant):
    if assistant.calendar is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Google Calendar is not configured. Add GOOGLE_CLIENT_ID and "
                "GOOGLE_CLIENT_SECRET to .env and restart Billie."
            ),
        )
    return assistant.calendar


def _handle(error: Exception) -> HTTPException:
    if isinstance(error, TokenStoreUnavailable):
        return HTTPException(status_code=501, detail=str(error))
    if isinstance(error, (OAuthError, CalendarError, ConfirmationError)):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=500, detail=str(error))


# --- connecting an account ------------------------------------------------


@router.get("/connection")
async def connection(assistant: Assistant = Depends(get_assistant)) -> dict:
    return assistant._calendar_status()


@router.post("/connect")
async def connect(request: Request, assistant: Assistant = Depends(get_assistant)) -> dict:
    """Return the Google sign-in URL to open in a browser."""
    calendar = _calendar(assistant)
    redirect_uri = str(request.base_url).rstrip("/") + CALLBACK_PATH
    try:
        url = calendar.oauth.authorisation_url(redirect_uri)
    except OAuthError as exc:
        raise _handle(exc)
    return {"authorisation_url": url, "redirect_uri": redirect_uri}


@callback_router.get("/oauth/callback", include_in_schema=False)
async def oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """Where Google sends the browser back after you approve access."""
    assistant: Assistant = request.app.state.assistant

    def page(title: str, body: str, ok: bool) -> HTMLResponse:
        colour = "#1a7f5a" if ok else "#b3261e"
        return HTMLResponse(
            f"""<!doctype html><html><head><meta charset="utf-8">
<title>Billie — {title}</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 34rem; margin: 15vh auto; padding: 0 1rem;">
<h1 style="color: {colour}; font-size: 1.25rem;">{title}</h1>
<p style="line-height: 1.6;">{body}</p>
<p style="color: #666; font-size: .9rem;">You can close this tab and go back to Billie.</p>
</body></html>""",
            status_code=200 if ok else 400,
        )

    if error:
        return page("Sign-in cancelled", f"Google reported: {error}", ok=False)
    if not code or not state:
        return page("Sign-in incomplete", "Google did not send a code back.", ok=False)
    if assistant.calendar is None:
        return page("Not configured", "Google Calendar is not configured.", ok=False)

    try:
        result = await assistant.calendar.oauth.exchange_code(code, state)
    except (OAuthError, TokenStoreUnavailable) as exc:
        return page("Could not connect", str(exc), ok=False)

    where = "your OS keychain" if result["storage"] == "os-keychain" else "a local file"
    return page(
        "Connected",
        f"Billie is connected to <strong>{result['email'] or 'your Google account'}</strong>. "
        f"The refresh token is stored in {where}.",
        ok=True,
    )


@router.post("/disconnect")
async def disconnect(assistant: Assistant = Depends(get_assistant)) -> dict:
    calendar = _calendar(assistant)
    result = await calendar.oauth.disconnect()
    # Events already pulled into memory go too.
    from billie.integrations.calendar_sync import CALENDAR_SOURCE_TYPE

    removed = 0
    for source_id in assistant.store.document_source_ids(CALENDAR_SOURCE_TYPE):
        if assistant.store.delete_document(source_id):
            removed += 1
    return {**result, "events_removed_from_memory": removed}


# --- reading --------------------------------------------------------------


@router.get("/calendars")
async def calendars(assistant: Assistant = Depends(get_assistant)) -> dict:
    assistant.permissions.require(CALENDAR_READ)
    try:
        return {"calendars": await _calendar(assistant).list_calendars()}
    except CalendarError as exc:
        raise _handle(exc)


@router.get("/events")
async def events(
    calendar_id: str = Query(default="primary"),
    days_ahead: int = Query(default=14, ge=1, le=365),
    days_past: int = Query(default=0, ge=0, le=365),
    assistant: Assistant = Depends(get_assistant),
) -> dict:
    assistant.permissions.require(CALENDAR_READ)
    try:
        found = await _calendar(assistant).list_events(
            calendar_id, days_past=days_past, days_ahead=days_ahead
        )
    except CalendarError as exc:
        raise _handle(exc)
    return {"calendar_id": calendar_id, "events": [event.to_dict() for event in found]}


@router.post("/sync")
async def sync(assistant: Assistant = Depends(get_assistant)) -> dict:
    """Pull events into memory so chat answers can cite them."""
    try:
        return (await assistant.sync_calendar()).to_dict()
    except CalendarError as exc:
        raise _handle(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# --- writing, via confirmation -------------------------------------------


@router.post("/events/propose")
async def propose_create(
    payload: CreateEventRequest, assistant: Assistant = Depends(get_assistant)
) -> dict:
    """Prepare an event. Nothing is sent to Google until this is approved."""
    assistant.permissions.require(CALENDAR_READ)
    try:
        action = await _calendar(assistant).propose_create(
            payload.summary,
            payload.start,
            payload.end,
            calendar_id=payload.calendar_id,
            all_day=payload.all_day,
            location=payload.location,
            description=payload.description,
            attendees=payload.attendees,
            timezone_name=payload.timezone,
        )
    except (CalendarError, ConfirmationError) as exc:
        raise _handle(exc)
    return {
        "action": action.to_dict(),
        "requires_permission": CALENDAR_WRITE,
        "permission_granted": assistant.permissions.is_granted(CALENDAR_WRITE),
    }


@router.post("/events/{event_id}/propose-move")
async def propose_move(
    event_id: str, payload: MoveEventRequest, assistant: Assistant = Depends(get_assistant)
) -> dict:
    assistant.permissions.require(CALENDAR_READ)
    try:
        action = await _calendar(assistant).propose_move(
            event_id,
            payload.start,
            payload.end,
            calendar_id=payload.calendar_id,
            all_day=payload.all_day,
            timezone_name=payload.timezone,
        )
    except (CalendarError, ConfirmationError) as exc:
        raise _handle(exc)
    return {"action": action.to_dict()}


@router.post("/events/{event_id}/propose-delete")
async def propose_delete(
    event_id: str,
    calendar_id: str = Query(default="primary"),
    assistant: Assistant = Depends(get_assistant),
) -> dict:
    assistant.permissions.require(CALENDAR_READ)
    try:
        action = await _calendar(assistant).propose_delete(event_id, calendar_id=calendar_id)
    except (CalendarError, ConfirmationError) as exc:
        raise _handle(exc)
    return {"action": action.to_dict()}


# --- the confirmation queue ----------------------------------------------


@router.get("/actions")
async def list_actions(
    all: bool = Query(default=False), assistant: Assistant = Depends(get_assistant)
) -> dict:
    actions = (
        assistant.confirmations.history("calendar.")
        if all
        else assistant.confirmations.pending("calendar.")
    )
    return {"actions": [action.to_dict() for action in actions]}


@router.post("/actions/{action_id}/approve")
async def approve(action_id: str, assistant: Assistant = Depends(get_assistant)) -> dict:
    """The only endpoint that changes your Google Calendar."""
    assistant.permissions.require(CALENDAR_WRITE)
    try:
        result = await assistant.confirmations.approve(action_id)
    except ConfirmationError as exc:
        raise _handle(exc)
    except CalendarError as exc:
        raise _handle(exc)
    # Keep memory in step with the calendar we just changed.
    if assistant.permissions.is_granted(CALENDAR_READ):
        try:
            await assistant.sync_calendar()
        except (CalendarError, RuntimeError):
            pass
    return result


@router.post("/actions/{action_id}/reject")
async def reject(
    action_id: str,
    payload: RejectRequest | None = None,
    assistant: Assistant = Depends(get_assistant),
) -> dict:
    try:
        return assistant.confirmations.reject(action_id, payload.reason if payload else "")
    except ConfirmationError as exc:
        raise _handle(exc)
