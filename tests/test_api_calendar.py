"""The calendar HTTP surface, including the OAuth callback."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from billie.permissions import CALENDAR_READ, CALENDAR_WRITE


def grant(client: TestClient, tool: str) -> None:
    assert client.post(f"/api/permissions/{tool}", json={"granted": True}).status_code == 200


# --- connecting -----------------------------------------------------------


def test_connect_returns_a_google_url_pointing_back_at_billie(client: TestClient):
    payload = client.post("/api/calendar/connect").json()
    parsed = urlparse(payload["authorisation_url"])
    assert parsed.netloc == "accounts.google.com"
    assert payload["redirect_uri"].endswith("/api/calendar/oauth/callback")


def test_the_callback_completes_the_sign_in(client: TestClient, assistant):
    # Start from disconnected.
    assistant.calendar.oauth.token_store.delete(assistant.calendar.oauth.account)
    url = client.post("/api/calendar/connect").json()["authorisation_url"]
    state = parse_qs(urlparse(url).query)["state"][0]

    response = client.get(
        "/api/calendar/oauth/callback", params={"code": "auth-code", "state": state}
    )
    assert response.status_code == 200
    assert "Connected" in response.text
    assert "ben@example.com" in response.text
    assert assistant.calendar.oauth.connected() is True


def test_the_callback_reports_a_cancelled_sign_in(client: TestClient):
    response = client.get("/api/calendar/oauth/callback", params={"error": "access_denied"})
    assert response.status_code == 400
    assert "cancelled" in response.text.lower()


def test_the_callback_rejects_a_bad_state(client: TestClient):
    client.post("/api/calendar/connect")
    response = client.get(
        "/api/calendar/oauth/callback", params={"code": "c", "state": "wrong"}
    )
    assert response.status_code == 400
    assert "Could not connect" in response.text


def test_the_connection_summary_has_no_secrets_in_it(client: TestClient):
    summary = client.get("/api/calendar/connection").json()
    assert summary["connected"] is True
    assert summary["email"] == "ben@example.com"
    assert "refresh" not in client.get("/api/calendar/connection").text


def test_disconnecting_revokes_and_clears_synced_events(client: TestClient, assistant, google):
    client.post("/api/calendar/sync")
    assert assistant.store.document_source_ids("calendar")

    result = client.post("/api/calendar/disconnect").json()
    assert result["revoked_with_google"] is True
    assert result["events_removed_from_memory"] == 1
    assert google.revoked == ["refresh-token-abc"]
    assert assistant.store.document_source_ids("calendar") == set()


# --- reading --------------------------------------------------------------


def test_events_can_be_listed(client: TestClient):
    events = client.get("/api/calendar/events").json()["events"]
    assert [event["summary"] for event in events] == ["Team standup"]


def test_reading_needs_the_calendar_read_permission(client: TestClient):
    client.post(f"/api/permissions/{CALENDAR_READ}", json={"granted": False})
    response = client.get("/api/calendar/events")
    assert response.status_code == 403
    assert response.json()["permission"] == CALENDAR_READ


def test_syncing_puts_events_into_memory_where_chat_can_cite_them(client: TestClient):
    report = client.post("/api/calendar/sync").json()
    assert report["synced"] == 1

    results = client.post(
        "/api/memory/search", json={"query": "team standup"}
    ).json()["results"]
    assert results[0]["source_id"] == "calendar/primary/evt_standup"


def test_revoking_calendar_read_removes_events_from_retrieval(client: TestClient):
    client.post("/api/calendar/sync")
    client.post(f"/api/permissions/{CALENDAR_READ}", json={"granted": False})

    results = client.post("/api/memory/search", json={"query": "team standup"}).json()["results"]
    assert all(result["source_type"] != "calendar" for result in results)


# --- writing --------------------------------------------------------------


def test_proposing_an_event_changes_nothing_yet(client: TestClient, google):
    before = dict(google.events)
    payload = client.post(
        "/api/calendar/events/propose",
        json={
            "summary": "Dentist",
            "start": "2027-04-01T09:00:00+00:00",
            "end": "2027-04-01T09:30:00+00:00",
        },
    ).json()

    assert payload["action"]["status"] == "pending"
    assert payload["permission_granted"] is False
    assert "CREATE" in payload["action"]["preview"]
    assert google.events == before


def test_approving_without_the_write_permission_is_refused(client: TestClient, google):
    action = client.post(
        "/api/calendar/events/propose",
        json={
            "summary": "Dentist",
            "start": "2027-04-01T09:00:00+00:00",
            "end": "2027-04-01T09:30:00+00:00",
        },
    ).json()["action"]

    response = client.post(f"/api/calendar/actions/{action['id']}/approve")
    assert response.status_code == 403
    assert response.json()["permission"] == CALENDAR_WRITE
    assert len(google.events) == 1, "nothing was created"


def test_the_full_propose_approve_cycle_creates_the_event(client: TestClient, google):
    grant(client, CALENDAR_WRITE)
    action = client.post(
        "/api/calendar/events/propose",
        json={
            "summary": "Dentist",
            "start": "2027-04-01T09:00:00+00:00",
            "end": "2027-04-01T09:30:00+00:00",
        },
    ).json()["action"]

    pending = client.get("/api/calendar/actions").json()["actions"]
    assert [entry["id"] for entry in pending] == [action["id"]]

    result = client.post(f"/api/calendar/actions/{action['id']}/approve").json()
    assert result["applied"] is True
    assert any(event["summary"] == "Dentist" for event in google.events.values())
    assert client.get("/api/calendar/actions").json()["actions"] == []


def test_rejecting_leaves_the_calendar_untouched(client: TestClient, google):
    grant(client, CALENDAR_WRITE)
    before = dict(google.events)
    action = client.post(
        "/api/calendar/events/propose",
        json={
            "summary": "Dentist",
            "start": "2027-04-01T09:00:00+00:00",
            "end": "2027-04-01T09:30:00+00:00",
        },
    ).json()["action"]

    rejected = client.post(
        f"/api/calendar/actions/{action['id']}/reject", json={"reason": "wrong day"}
    )
    assert rejected.status_code == 200
    assert google.events == before


def test_moving_an_event_over_http(client: TestClient, google):
    grant(client, CALENDAR_WRITE)
    action = client.post(
        "/api/calendar/events/evt_standup/propose-move",
        json={"start": "2027-03-14T11:00:00+00:00", "end": "2027-03-14T11:15:00+00:00"},
    ).json()["action"]
    assert google.events["evt_standup"]["start"]["dateTime"] == "2027-03-14T09:00:00+00:00"

    client.post(f"/api/calendar/actions/{action['id']}/approve")
    assert google.events["evt_standup"]["start"]["dateTime"] == "2027-03-14T11:00:00+00:00"


def test_deleting_an_event_over_http(client: TestClient, google):
    grant(client, CALENDAR_WRITE)
    action = client.post(
        "/api/calendar/events/evt_standup/propose-delete"
    ).json()["action"]
    assert "evt_standup" in google.events

    client.post(f"/api/calendar/actions/{action['id']}/approve")
    assert "evt_standup" not in google.events


def test_an_approved_change_is_audited_with_what_happened(client: TestClient):
    grant(client, CALENDAR_WRITE)
    action = client.post(
        "/api/calendar/events/propose",
        json={
            "summary": "Dentist",
            "start": "2027-04-01T09:00:00+00:00",
            "end": "2027-04-01T09:30:00+00:00",
        },
    ).json()["action"]
    client.post(f"/api/calendar/actions/{action['id']}/approve")

    events = [entry["event"] for entry in client.get("/api/audit?limit=200").json()["entries"]]
    assert "confirmation.proposed" in events
    assert "confirmation.approved" in events
    assert "google.api_call" in events


def test_status_reports_the_calendar_connection(client: TestClient):
    status = client.get("/api/status").json()
    assert status["calendar"]["connected"] is True
    assert status["calendar"]["writable"] is False
    assert status["calendar"]["storage"]["backend"] in {"os-keychain", "file"}


# --- not configured -------------------------------------------------------


def test_without_google_credentials_the_endpoints_say_so(assistant, settings):
    from billie.app import create_app

    assistant.calendar = None
    app = create_app(settings, assistant, serve_frontend=False)
    with TestClient(app, client=("127.0.0.1", 51234)) as unconfigured:
        response = unconfigured.post("/api/calendar/connect")
        assert response.status_code == 503
        assert "GOOGLE_CLIENT_ID" in response.json()["detail"]
        assert unconfigured.get("/api/status").json()["calendar"]["configured"] is False
