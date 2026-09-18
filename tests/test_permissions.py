"""Per-tool permission grants."""

from __future__ import annotations

import pytest

from billie.permissions import (
    FILE_READ,
    FILE_WRITE,
    REGISTRY,
    PermissionDenied,
    Permissions,
)


def test_defaults_are_read_yes_write_no(permissions: Permissions):
    assert permissions.is_granted(FILE_READ) is True
    assert permissions.is_granted(FILE_WRITE) is False


def test_every_registered_tool_is_described_for_the_ui(permissions: Permissions):
    state = {entry["tool"]: entry for entry in permissions.state()}
    assert set(state) == set(REGISTRY)
    for entry in state.values():
        assert entry["title"] and entry["description"]
        assert entry["explicitly_set"] is False


def test_granting_and_revoking_persists(permissions: Permissions, store):
    permissions.set(FILE_WRITE, True, note="drafting session")
    assert permissions.is_granted(FILE_WRITE) is True

    # A fresh Permissions object over the same store sees the same state.
    reloaded = Permissions(store, permissions.audit)
    assert reloaded.is_granted(FILE_WRITE) is True
    description = reloaded.describe(FILE_WRITE)
    assert description["explicitly_set"] is True
    assert description["note"] == "drafting session"

    permissions.set(FILE_WRITE, False, note="done for today")
    assert reloaded.is_granted(FILE_WRITE) is False


def test_each_tool_is_revoked_independently(permissions: Permissions):
    permissions.set(FILE_READ, False)
    assert permissions.is_granted(FILE_READ) is False
    assert permissions.is_granted(FILE_WRITE) is False

    permissions.set(FILE_WRITE, True)
    assert permissions.is_granted(FILE_READ) is False
    assert permissions.is_granted(FILE_WRITE) is True


def test_require_raises_with_the_tool_name(permissions: Permissions):
    permissions.require(FILE_READ)  # granted by default, no raise
    with pytest.raises(PermissionDenied) as excinfo:
        permissions.require(FILE_WRITE)
    assert excinfo.value.tool == FILE_WRITE
    assert FILE_WRITE in str(excinfo.value)


def test_every_change_is_audited(permissions: Permissions):
    permissions.set(FILE_WRITE, True, note="for this draft")
    permissions.set(FILE_WRITE, False)
    with pytest.raises(PermissionDenied):
        permissions.require(FILE_WRITE)

    events = [entry["event"] for entry in permissions.audit.tail()]
    assert events[-3:] == [
        "permission.granted",
        "permission.revoked",
        "permission.denied",
    ]


def test_an_unknown_tool_is_rejected(permissions: Permissions):
    with pytest.raises(KeyError):
        permissions.is_granted("file.delete_everything")
    with pytest.raises(KeyError):
        permissions.set("nope", True)


def test_revoking_file_read_stops_indexing(assistant):
    (assistant.settings.notes_dir / "a.md").write_text("A note.", encoding="utf-8")
    assistant.permissions.set(FILE_READ, False)
    with pytest.raises(PermissionDenied):
        assistant.reindex_files()


def test_revoking_file_read_removes_files_from_retrieval(assistant):
    (assistant.settings.notes_dir / "kit.md").write_text(
        "The microphone is a Shure SM7B.", encoding="utf-8"
    )
    assistant.reindex_files()
    assistant.teach("My studio monitor is a Yamaha HS5.", title="Monitor")

    assert any(chunk.source_type == "file" for chunk in assistant.search("microphone"))

    assistant.permissions.set(FILE_READ, False)
    results = assistant.search("microphone")
    assert all(chunk.source_type != "file" for chunk in results)
    # Facts taught directly live in Billie's own database, so they remain.
    assert any(chunk.source_type == "fact" for chunk in assistant.search("studio monitor"))
