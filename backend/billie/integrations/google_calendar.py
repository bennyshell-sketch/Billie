"""Google Calendar: read freely, write only with your confirmation.

Reads (listing calendars, fetching events) happen directly. Every write --
creating an event, moving one, deleting one -- goes through the confirmation
queue: Billie builds a preview showing exactly what will change, and nothing is
sent to Google until you approve it.

This talks to the Calendar REST API with httpx rather than the Google client
libraries: it is a handful of endpoints, and it keeps the dependency list and
the amount of hidden behaviour small.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from billie.audit import AuditLog
from billie.confirmations import ConfirmationQueue
from billie.integrations.google_oauth import GoogleOAuth, OAuthError

logger = logging.getLogger("billie.calendar")

API_BASE = "https://www.googleapis.com/calendar/v3"

KIND_CREATE = "calendar.create"
KIND_MOVE = "calendar.move"
KIND_DELETE = "calendar.delete"
KIND_PREFIX = "calendar."


class CalendarError(RuntimeError):
    """User-facing message about a calendar operation."""


@dataclass
class Event:
    id: str
    calendar_id: str
    summary: str
    start: str
    end: str
    all_day: bool
    location: str = ""
    description: str = ""
    attendees: list[str] | None = None
    link: str = ""
    updated: str = ""

    @classmethod
    def from_api(cls, payload: dict[str, Any], calendar_id: str) -> "Event":
        start = payload.get("start") or {}
        end = payload.get("end") or {}
        all_day = "date" in start
        return cls(
            id=payload.get("id", ""),
            calendar_id=calendar_id,
            summary=payload.get("summary") or "(no title)",
            start=start.get("dateTime") or start.get("date") or "",
            end=end.get("dateTime") or end.get("date") or "",
            all_day=all_day,
            location=payload.get("location") or "",
            description=payload.get("description") or "",
            attendees=[
                attendee.get("email", "")
                for attendee in payload.get("attendees", [])
                if attendee.get("email")
            ],
            link=payload.get("htmlLink") or "",
            updated=payload.get("updated") or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "calendar_id": self.calendar_id,
            "summary": self.summary,
            "start": self.start,
            "end": self.end,
            "all_day": self.all_day,
            "location": self.location,
            "description": self.description,
            "attendees": self.attendees or [],
            "link": self.link,
        }

    def describe(self) -> str:
        """One line, the way it is shown in a confirmation preview."""
        when = _format_span(self.start, self.end, self.all_day)
        parts = [f"{self.summary} — {when}"]
        if self.location:
            parts.append(f"at {self.location}")
        if self.attendees:
            parts.append(f"with {', '.join(self.attendees)}")
        return " ".join(parts)

    def as_document(self) -> str:
        """The text form that goes into memory, so answers can cite it."""
        lines = [f"# {self.summary}", "", _format_span(self.start, self.end, self.all_day)]
        if self.location:
            lines.append(f"Location: {self.location}")
        if self.attendees:
            lines.append(f"Attendees: {', '.join(self.attendees)}")
        if self.description:
            lines.extend(["", self.description.strip()])
        return "\n".join(lines)


def _format_span(start: str, end: str, all_day: bool) -> str:
    if all_day:
        if start == end or not end:
            return f"all day on {start}"
        return f"all day, {start} to {end}"
    try:
        start_at = datetime.fromisoformat(start)
        end_at = datetime.fromisoformat(end)
    except ValueError:
        return f"{start} to {end}"
    same_day = start_at.date() == end_at.date()
    if same_day:
        return (
            f"{start_at.strftime('%a %d %b %Y, %H:%M')}–{end_at.strftime('%H:%M')}"
        )
    return (
        f"{start_at.strftime('%a %d %b %Y, %H:%M')} to "
        f"{end_at.strftime('%a %d %b %Y, %H:%M')}"
    )


def _time_field(value: str, all_day: bool, timezone_name: str | None) -> dict[str, str]:
    if all_day:
        return {"date": value[:10]}
    field = {"dateTime": value}
    if timezone_name:
        field["timeZone"] = timezone_name
    return field


class GoogleCalendar:
    def __init__(
        self,
        oauth: GoogleOAuth,
        confirmations: ConfirmationQueue,
        audit: AuditLog,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.oauth = oauth
        self.confirmations = confirmations
        self.audit = audit
        self._client = client
        confirmations.register(KIND_CREATE, self._execute_create)
        confirmations.register(KIND_MOVE, self._execute_move)
        confirmations.register(KIND_DELETE, self._execute_delete)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _http(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(timeout=30.0)

    async def _request(
        self, method: str, path: str, *, params: dict | None = None, json: dict | None = None
    ) -> dict[str, Any]:
        try:
            token = await self.oauth.access_token()
        except OAuthError as exc:
            raise CalendarError(str(exc)) from exc

        client = self._http()
        try:
            response = await client.request(
                method,
                f"{API_BASE}{path}",
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise CalendarError(f"Could not reach Google Calendar ({exc}).") from exc
        finally:
            if self._client is None:
                await client.aclose()

        self.audit.record(
            "google.api_call",
            method=method,
            path=path,
            status=response.status_code,
        )

        if response.status_code == 204 or not response.content:
            return {}
        if response.status_code >= 400:
            detail = ""
            try:
                detail = response.json().get("error", {}).get("message", "")
            except ValueError:
                detail = response.text[:200]
            if response.status_code in (401, 403):
                raise CalendarError(
                    f"Google refused the request ({response.status_code}): {detail}. "
                    "You may need to reconnect the account."
                )
            if response.status_code == 404:
                raise CalendarError("That calendar or event no longer exists.")
            raise CalendarError(f"Google Calendar error ({response.status_code}): {detail}")
        return response.json()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def list_calendars(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/users/me/calendarList")
        return [
            {
                "id": item.get("id"),
                "summary": item.get("summary"),
                "primary": bool(item.get("primary")),
                "timezone": item.get("timeZone"),
                "access_role": item.get("accessRole"),
            }
            for item in payload.get("items", [])
        ]

    async def list_events(
        self,
        calendar_id: str = "primary",
        *,
        days_past: int = 1,
        days_ahead: int = 30,
        max_results: int = 250,
    ) -> list[Event]:
        now = datetime.now(timezone.utc)
        params = {
            "timeMin": (now - timedelta(days=days_past)).isoformat(),
            "timeMax": (now + timedelta(days=days_ahead)).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(max_results),
        }
        payload = await self._request(
            "GET", f"/calendars/{calendar_id}/events", params=params
        )
        return [Event.from_api(item, calendar_id) for item in payload.get("items", [])]

    async def get_event(self, event_id: str, calendar_id: str = "primary") -> Event:
        payload = await self._request("GET", f"/calendars/{calendar_id}/events/{event_id}")
        return Event.from_api(payload, calendar_id)

    # ------------------------------------------------------------------
    # Writes -- proposed, never immediate
    # ------------------------------------------------------------------
    async def propose_create(
        self,
        summary: str,
        start: str,
        end: str,
        *,
        calendar_id: str = "primary",
        all_day: bool = False,
        location: str = "",
        description: str = "",
        attendees: list[str] | None = None,
        timezone_name: str | None = None,
    ):
        if not summary.strip():
            raise CalendarError("An event needs a title.")
        preview_event = Event(
            id="",
            calendar_id=calendar_id,
            summary=summary,
            start=start,
            end=end,
            all_day=all_day,
            location=location,
            description=description,
            attendees=attendees or [],
        )
        preview_lines = [
            "Billie will CREATE this event:",
            "",
            f"  {preview_event.describe()}",
            f"  calendar: {calendar_id}",
        ]
        if description:
            preview_lines.append(f"  notes: {description.strip()[:200]}")
        if attendees:
            preview_lines.append(
                "  Invitations will be emailed to: " + ", ".join(attendees)
            )
        return self.confirmations.propose(
            KIND_CREATE,
            summary=f"Create “{summary}” — {_format_span(start, end, all_day)}",
            preview="\n".join(preview_lines),
            payload={
                "calendar_id": calendar_id,
                "summary": summary,
                "start": start,
                "end": end,
                "all_day": all_day,
                "location": location,
                "description": description,
                "attendees": attendees or [],
                "timezone_name": timezone_name,
            },
        )

    async def propose_move(
        self,
        event_id: str,
        start: str,
        end: str,
        *,
        calendar_id: str = "primary",
        all_day: bool | None = None,
        timezone_name: str | None = None,
    ):
        existing = await self.get_event(event_id, calendar_id)
        is_all_day = existing.all_day if all_day is None else all_day
        preview = "\n".join(
            [
                "Billie will MOVE this event:",
                "",
                f"  {existing.summary}",
                f"  from: {_format_span(existing.start, existing.end, existing.all_day)}",
                f"    to: {_format_span(start, end, is_all_day)}",
                f"  calendar: {calendar_id}",
                "",
                "Anyone invited will be notified by Google.",
            ]
        )
        return self.confirmations.propose(
            KIND_MOVE,
            summary=f"Move “{existing.summary}” to {_format_span(start, end, is_all_day)}",
            preview=preview,
            payload={
                "calendar_id": calendar_id,
                "event_id": event_id,
                "start": start,
                "end": end,
                "all_day": is_all_day,
                "timezone_name": timezone_name,
                "previous": {"start": existing.start, "end": existing.end},
                "summary": existing.summary,
            },
        )

    async def propose_delete(self, event_id: str, *, calendar_id: str = "primary"):
        existing = await self.get_event(event_id, calendar_id)
        preview = "\n".join(
            [
                "Billie will DELETE this event:",
                "",
                f"  {existing.describe()}",
                f"  calendar: {calendar_id}",
                "",
                "This cannot be undone from Billie. Anyone invited will be notified.",
            ]
        )
        return self.confirmations.propose(
            KIND_DELETE,
            summary=f"Delete “{existing.summary}” ({_format_span(existing.start, existing.end, existing.all_day)})",
            preview=preview,
            payload={
                "calendar_id": calendar_id,
                "event_id": event_id,
                "summary": existing.summary,
            },
        )

    # ------------------------------------------------------------------
    # Executors -- only reached through an approved confirmation
    # ------------------------------------------------------------------
    async def _execute_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary": payload["summary"],
            "start": _time_field(payload["start"], payload["all_day"], payload.get("timezone_name")),
            "end": _time_field(payload["end"], payload["all_day"], payload.get("timezone_name")),
        }
        if payload.get("location"):
            body["location"] = payload["location"]
        if payload.get("description"):
            body["description"] = payload["description"]
        if payload.get("attendees"):
            body["attendees"] = [{"email": email} for email in payload["attendees"]]

        created = await self._request(
            "POST", f"/calendars/{payload['calendar_id']}/events", json=body
        )
        event = Event.from_api(created, payload["calendar_id"])
        return {"event_id": event.id, "link": event.link, "summary": event.summary}

    async def _execute_move(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = {
            "start": _time_field(payload["start"], payload["all_day"], payload.get("timezone_name")),
            "end": _time_field(payload["end"], payload["all_day"], payload.get("timezone_name")),
        }
        updated = await self._request(
            "PATCH",
            f"/calendars/{payload['calendar_id']}/events/{payload['event_id']}",
            json=body,
        )
        event = Event.from_api(updated, payload["calendar_id"])
        return {"event_id": event.id, "link": event.link, "start": event.start, "end": event.end}

    async def _execute_delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self._request(
            "DELETE", f"/calendars/{payload['calendar_id']}/events/{payload['event_id']}"
        )
        return {"event_id": payload["event_id"], "deleted": True}
