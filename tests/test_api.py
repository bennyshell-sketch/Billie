"""The HTTP API, including auth and the SSE stream."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from billie.app import create_app


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        event, data = "message", ""
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if data:
            events.append((event, json.loads(data)))
    return events


def test_health_needs_no_auth(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_chat_returns_answer_with_routing_and_grounding(client: TestClient):
    response = client.post("/api/chat", json={"message": "hi"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["answer"] == "Local answer."
    assert payload["routing"]["kind"] == "local"
    assert payload["routing"]["model"] == "llama3.1:8b"
    assert "confidence" in payload["grounding"]


def test_chat_rejects_an_empty_message(client: TestClient):
    assert client.post("/api/chat", json={"message": ""}).status_code == 422


def test_chat_stream_emits_sse_frames(client: TestClient):
    response = client.post("/api/chat/stream", json={"message": "hi"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[0] == "start" and names[-1] == "final"
    final = events[-1][1]
    assert final["answer"] == "Local answer."
    assert final["routing"]["kind"] == "local"


def test_stream_reports_provider_failure_as_an_error_event(client: TestClient, assistant):
    assistant.router.local.healthy = False
    assistant.router.cloud.healthy = False
    response = client.post("/api/chat/stream", json={"message": "hi"})
    events = parse_sse(response.text)
    assert events[-1][0] == "error"
    assert "No model available" in events[-1][1]["error"]


def test_chat_returns_503_when_no_model_is_available(client: TestClient, assistant):
    assistant.router.local.healthy = False
    assistant.router.cloud.healthy = False
    response = client.post("/api/chat", json={"message": "hi"})
    assert response.status_code == 503


def test_private_flag_is_honoured_over_http(client: TestClient, assistant):
    response = client.post("/api/chat", json={"message": "sensitive", "private": True})
    assert response.json()["routing"]["kind"] == "local"
    assert assistant.router.cloud.calls == []


def test_forced_cloud_route_over_http(client: TestClient):
    response = client.post("/api/chat", json={"message": "hi", "route": "cloud"})
    payload = response.json()
    assert payload["routing"]["kind"] == "cloud"
    assert payload["answer"] == "Cloud answer."


def test_memory_endpoints_teach_search_and_reindex(client: TestClient, assistant):
    taught = client.post(
        "/api/memory/teach",
        json={"content": "My passport expires in March 2031.", "title": "Passport"},
    )
    assert taught.status_code == 200

    found = client.post("/api/memory/search", json={"query": "when does my passport expire"})
    assert found.json()["results"][0]["title"] == "Passport"

    (assistant.settings.notes_dir / "n.md").write_text("# N\n\nA note.", encoding="utf-8")
    reindexed = client.post("/api/memory/reindex")
    assert reindexed.json()["indexed"] == 1

    documents = client.get("/api/memory/documents").json()["documents"]
    assert {doc["source_type"] for doc in documents} == {"fact", "note"}


def test_conversations_can_be_listed_and_replayed(client: TestClient):
    conversation_id = client.post("/api/chat", json={"message": "hi"}).json()["conversation_id"]
    listed = client.get("/api/conversations").json()["conversations"]
    assert listed[0]["id"] == conversation_id
    messages = client.get(f"/api/conversations/{conversation_id}").json()["messages"]
    assert len(messages) == 2
    assert client.get("/api/conversations/cnv_missing").status_code == 404


def test_status_and_audit_endpoints(client: TestClient):
    client.post("/api/chat", json={"message": "hi"})
    status = client.get("/api/status").json()
    assert status["providers"]["cloud"]["model"] == "claude-opus-5"

    entries = client.get("/api/audit?limit=10").json()["entries"]
    assert any(entry["event"] == "chat.response" for entry in entries)


def test_audit_endpoint_never_leaks_a_key(client: TestClient, assistant):
    fake_key = "sk-" + "ant-api03-" + "SHOULDNOTAPPEAR" * 2
    assistant.audit.record("external.call", api_key=fake_key)
    body = client.get("/api/audit").text
    assert "SHOULDNOTAPPEAR" not in body
    assert "[redacted]" in body


# --- auth ----------------------------------------------------------------


def _client_with_token(assistant, settings, token: str, *, loopback_exempt: bool):
    settings.api_token = token
    settings.allow_loopback_unauthenticated = loopback_exempt
    return TestClient(
        create_app(settings, assistant, serve_frontend=False), client=("127.0.0.1", 51234)
    )


def test_loopback_is_exempt_when_configured(assistant, settings):
    with _client_with_token(assistant, settings, "s3cret", loopback_exempt=True) as client:
        assert client.post("/api/chat", json={"message": "hi"}).status_code == 200


def test_a_token_is_required_when_loopback_is_not_exempt(assistant, settings):
    with _client_with_token(assistant, settings, "s3cret", loopback_exempt=False) as client:
        assert client.post("/api/chat", json={"message": "hi"}).status_code == 401
        assert client.get("/api/status").status_code == 401

        ok = client.post(
            "/api/chat",
            json={"message": "hi"},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert ok.status_code == 200

        wrong = client.post(
            "/api/chat", json={"message": "hi"}, headers={"Authorization": "Bearer nope"}
        )
        assert wrong.status_code == 401


def test_health_stays_open_even_with_auth_enabled(assistant, settings):
    with _client_with_token(assistant, settings, "s3cret", loopback_exempt=False) as client:
        assert client.get("/api/health").status_code == 200


def test_non_loopback_client_without_a_token_is_refused(assistant, settings):
    settings.api_token = None
    app = create_app(settings, assistant, serve_frontend=False)
    with TestClient(app, client=("203.0.113.9", 55000)) as client:
        response = client.post("/api/chat", json={"message": "hi"})
        assert response.status_code == 401
        assert "BILLIE_API_TOKEN" in response.json()["detail"]
