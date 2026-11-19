"""An in-memory stand-in for Google's OAuth and Calendar endpoints.

Lets the whole flow -- sign in, refresh, list, create, move, delete -- run in
tests over httpx's mock transport, with no network and no real account.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.parse import parse_qs

import httpx


class FakeGoogle:
    def __init__(self, email: str = "ben@example.com") -> None:
        self.email = email
        self.calendars: list[dict[str, Any]] = [
            {
                "id": "primary",
                "summary": "Ben",
                "primary": True,
                "timeZone": "Europe/London",
                "accessRole": "owner",
            }
        ]
        self.events: dict[str, dict[str, Any]] = {}
        self.token_requests: list[dict[str, str]] = []
        self.revoked: list[str] = []
        self.api_calls: list[tuple[str, str]] = []
        self.refresh_token_on_exchange: str | None = "refresh-token-abc"
        self.access_token = "access-token-1"
        self.expires_in = 3600
        self.fail_next_with: tuple[int, str] | None = None

    # ------------------------------------------------------------------
    def add_event(self, **fields: Any) -> dict[str, Any]:
        event_id = fields.pop("id", f"evt_{uuid.uuid4().hex[:10]}")
        event = {
            "id": event_id,
            "summary": fields.pop("summary", "Untitled"),
            "start": fields.pop("start", {"dateTime": "2027-03-14T09:00:00Z"}),
            "end": fields.pop("end", {"dateTime": "2027-03-14T10:00:00Z"}),
            "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
            "updated": "2027-03-01T00:00:00Z",
            **fields,
        }
        self.events[event_id] = event
        return event

    # ------------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = request.url.path
        self.api_calls.append((request.method, path))

        if self.fail_next_with is not None:
            status, message = self.fail_next_with
            self.fail_next_with = None
            return httpx.Response(status, json={"error": {"message": message}})

        if url.startswith("https://oauth2.googleapis.com/token"):
            return self._token(request)
        if url.startswith("https://oauth2.googleapis.com/revoke"):
            form = parse_qs(request.content.decode())
            self.revoked.append(form.get("token", [""])[0])
            return httpx.Response(200, json={})
        if url.startswith("https://www.googleapis.com/oauth2/v3/userinfo"):
            return httpx.Response(200, json={"email": self.email})

        if path == "/calendar/v3/users/me/calendarList":
            return httpx.Response(200, json={"items": self.calendars})

        if path.startswith("/calendar/v3/calendars/"):
            return self._calendar(request, path)

        return httpx.Response(404, json={"error": {"message": f"no route for {path}"}})

    # ------------------------------------------------------------------
    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        self.token_requests.append(form)
        payload: dict[str, Any] = {
            "access_token": self.access_token,
            "expires_in": self.expires_in,
            "token_type": "Bearer",
            "scope": (
                "https://www.googleapis.com/auth/calendar.readonly "
                "https://www.googleapis.com/auth/calendar.events"
            ),
        }
        if form.get("grant_type") == "authorization_code" and self.refresh_token_on_exchange:
            payload["refresh_token"] = self.refresh_token_on_exchange
        return httpx.Response(200, json=payload)

    def _calendar(self, request: httpx.Request, path: str) -> httpx.Response:
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            return httpx.Response(401, json={"error": {"message": "no credentials"}})

        remainder = path[len("/calendar/v3/calendars/") :]
        calendar_id, _, rest = remainder.partition("/events")
        event_id = rest.lstrip("/")

        if request.method == "GET" and not event_id:
            return httpx.Response(
                200,
                json={
                    "items": sorted(
                        self.events.values(),
                        key=lambda event: event["start"].get("dateTime")
                        or event["start"].get("date", ""),
                    )
                },
            )
        if request.method == "GET":
            event = self.events.get(event_id)
            if event is None:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            return httpx.Response(200, json=event)
        if request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(200, json=self.add_event(**body))
        if request.method == "PATCH":
            event = self.events.get(event_id)
            if event is None:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            event.update(json.loads(request.content))
            return httpx.Response(200, json=event)
        if request.method == "DELETE":
            if event_id not in self.events:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            del self.events[event_id]
            return httpx.Response(204)
        return httpx.Response(405, json={"error": {"message": "method not allowed"}})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
