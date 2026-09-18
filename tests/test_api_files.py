"""HTTP surface for permissions and workspace writes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from billie.permissions import FILE_READ, FILE_WRITE


def grant_write(client: TestClient) -> None:
    response = client.post(f"/api/permissions/{FILE_WRITE}", json={"granted": True})
    assert response.status_code == 200


# --- permissions ----------------------------------------------------------


def test_permissions_are_listed_with_their_descriptions(client: TestClient):
    permissions = client.get("/api/permissions").json()["permissions"]
    by_tool = {entry["tool"]: entry for entry in permissions}
    assert by_tool[FILE_READ]["granted"] is True
    assert by_tool[FILE_WRITE]["granted"] is False
    assert by_tool[FILE_WRITE]["description"]


def test_a_permission_can_be_granted_and_revoked_over_http(client: TestClient):
    granted = client.post(
        f"/api/permissions/{FILE_WRITE}", json={"granted": True, "note": "drafting"}
    ).json()
    assert granted["granted"] is True and granted["note"] == "drafting"

    revoked = client.post(f"/api/permissions/{FILE_WRITE}", json={"granted": False}).json()
    assert revoked["granted"] is False


def test_an_unknown_permission_is_a_404(client: TestClient):
    assert client.post("/api/permissions/file.nope", json={"granted": True}).status_code == 404


def test_reindex_needs_the_read_permission(client: TestClient):
    client.post(f"/api/permissions/{FILE_READ}", json={"granted": False})
    response = client.post("/api/memory/reindex")
    assert response.status_code == 403
    assert response.json()["permission"] == FILE_READ


# --- the write flow -------------------------------------------------------


def test_propose_returns_a_diff_and_writes_nothing(client: TestClient, assistant):
    response = client.post(
        "/api/workspace/propose",
        json={"path": "plan.md", "content": "# Plan\n\nStep one.\n", "note": "first draft"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["proposal"]["action"] == "create"
    assert "+# Plan" in payload["proposal"]["diff"]
    assert payload["permission_granted"] is False, "write is not granted by default"
    assert not (assistant.workspace.root / "plan.md").exists()


def test_approval_is_refused_without_the_write_permission(client: TestClient, assistant):
    proposal = client.post(
        "/api/workspace/propose", json={"path": "plan.md", "content": "x\n"}
    ).json()["proposal"]

    response = client.post(f"/api/workspace/proposals/{proposal['id']}/approve")
    assert response.status_code == 403
    assert response.json()["permission"] == FILE_WRITE
    assert not (assistant.workspace.root / "plan.md").exists()


def test_the_full_propose_approve_cycle(client: TestClient, assistant):
    grant_write(client)
    proposal = client.post(
        "/api/workspace/propose",
        json={"path": "drafts/plan.md", "content": "# Plan\n\nWrite the thing.\n"},
    ).json()["proposal"]

    pending = client.get("/api/workspace/proposals").json()["proposals"]
    assert [entry["id"] for entry in pending] == [proposal["id"]]

    approved = client.post(f"/api/workspace/proposals/{proposal['id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["applied"] is True

    written = assistant.workspace.root / "drafts" / "plan.md"
    assert written.read_text(encoding="utf-8") == "# Plan\n\nWrite the thing.\n"
    assert client.get("/api/workspace/proposals").json()["proposals"] == []

    # The new file is indexed straight away, and cited as a workspace file.
    results = client.post(
        "/api/memory/search", json={"query": "write the thing"}
    ).json()["results"]
    assert any(result["source_id"] == "workspace/drafts/plan.md" for result in results)


def test_rejecting_over_http_leaves_the_file_alone(client: TestClient, assistant):
    grant_write(client)
    (assistant.workspace.root / "plan.md").write_text("original\n", encoding="utf-8")
    proposal = client.post(
        "/api/workspace/propose", json={"path": "plan.md", "content": "replaced\n"}
    ).json()["proposal"]

    rejected = client.post(
        f"/api/workspace/proposals/{proposal['id']}/reject",
        json={"reason": "wrong file"},
    )
    assert rejected.status_code == 200
    assert (assistant.workspace.root / "plan.md").read_text(encoding="utf-8") == "original\n"


def test_a_stale_approval_returns_409(client: TestClient, assistant):
    grant_write(client)
    target = assistant.workspace.root / "plan.md"
    target.write_text("version one\n", encoding="utf-8")
    proposal = client.post(
        "/api/workspace/propose", json={"path": "plan.md", "content": "version two\n"}
    ).json()["proposal"]

    target.write_text("edited by hand\n", encoding="utf-8")
    response = client.post(f"/api/workspace/proposals/{proposal['id']}/approve")
    assert response.status_code == 409
    assert target.read_text(encoding="utf-8") == "edited by hand\n"


def test_a_path_outside_the_workspace_is_a_400(client: TestClient):
    response = client.post(
        "/api/workspace/propose", json={"path": "../../escape.md", "content": "x"}
    )
    assert response.status_code == 400
    assert "outside the workspace" in response.json()["detail"]


def test_deleting_needs_the_same_approval(client: TestClient, assistant):
    grant_write(client)
    target = assistant.workspace.root / "old.md"
    target.write_text("delete me\n", encoding="utf-8")

    proposal = client.post(
        "/api/workspace/propose-delete", json={"path": "old.md"}
    ).json()["proposal"]
    assert target.exists(), "proposing a delete must not delete"

    client.post(f"/api/workspace/proposals/{proposal['id']}/approve")
    assert not target.exists()


def test_workspace_files_can_be_listed_and_read(client: TestClient, assistant):
    (assistant.workspace.root / "a.md").write_text("hello\n", encoding="utf-8")
    listed = client.get("/api/workspace/files").json()
    assert [entry["path"] for entry in listed["files"]] == ["a.md"]
    assert client.get("/api/workspace/file", params={"path": "a.md"}).json()["content"] == "hello\n"
    assert client.get("/api/workspace/file", params={"path": "nope.md"}).status_code == 400


def test_writes_show_up_in_the_audit_log(client: TestClient):
    grant_write(client)
    proposal = client.post(
        "/api/workspace/propose", json={"path": "plan.md", "content": "x\n"}
    ).json()["proposal"]
    client.post(f"/api/workspace/proposals/{proposal['id']}/approve")

    events = [entry["event"] for entry in client.get("/api/audit").json()["entries"]]
    assert "permission.granted" in events
    assert "workspace.proposed" in events
    assert "workspace.write" in events


def test_status_reports_permissions_and_indexed_roots(client: TestClient):
    status = client.get("/api/status").json()
    assert {entry["tool"] for entry in status["permissions"]} == {FILE_READ, FILE_WRITE}
    assert status["files"]["workspace_writable"] is False
    assert any(root["name"] == "notes" for root in status["files"]["roots"])
