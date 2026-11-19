"""Workspace writes: propose a diff, approve it, only then does anything change."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from billie.workspace import (
    MAX_WRITE_BYTES,
    PathOutsideWorkspace,
    ProposalConflict,
    Workspace,
    WorkspaceError,
)


# --- path confinement -----------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../escape.md",
        "../../etc/passwd",
        "notes/../../escape.md",
        "/etc/passwd",
        "~/secrets.md",
    ],
)
def test_paths_outside_the_workspace_are_refused(workspace: Workspace, path):
    with pytest.raises(PathOutsideWorkspace):
        workspace.resolve(path)


def test_nested_paths_inside_the_workspace_are_fine(workspace: Workspace):
    resolved = workspace.resolve("drafts/2026/plan.md")
    assert resolved.is_relative_to(workspace.root)


def test_the_workspace_folder_itself_is_not_a_file(workspace: Workspace):
    with pytest.raises(WorkspaceError):
        workspace.resolve(".")
    with pytest.raises(WorkspaceError):
        workspace.resolve("")


@pytest.mark.skipif(os.name == "nt", reason="symlinks need elevation on Windows")
def test_a_symlink_pointing_outside_is_refused(workspace: Workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace.root / "escape").symlink_to(outside)
    with pytest.raises(PathOutsideWorkspace):
        workspace.resolve("escape/secret.md")


# --- proposing ------------------------------------------------------------


def test_proposing_a_new_file_writes_nothing(workspace: Workspace):
    proposal = workspace.propose_write("plan.md", "# Plan\n\nStep one.\n", note="draft")
    assert proposal.action == "create"
    assert proposal.status == "pending"
    assert not (workspace.root / "plan.md").exists(), "propose must not write"
    assert "+# Plan" in proposal.diff
    assert "/dev/null" in proposal.diff


def test_proposing_an_edit_shows_a_real_diff(workspace: Workspace):
    target = workspace.root / "plan.md"
    target.write_text("# Plan\n\nStep one.\n", encoding="utf-8")

    proposal = workspace.propose_write("plan.md", "# Plan\n\nStep one.\nStep two.\n")
    assert proposal.action == "update"
    assert "+Step two." in proposal.diff
    assert "--- a/plan.md" in proposal.diff and "+++ b/plan.md" in proposal.diff
    assert target.read_text(encoding="utf-8") == "# Plan\n\nStep one.\n"


def test_an_identical_write_is_rejected_as_pointless(workspace: Workspace):
    (workspace.root / "plan.md").write_text("same\n", encoding="utf-8")
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.propose_write("plan.md", "same\n")
    assert "already has exactly this content" in str(excinfo.value)


def test_an_oversized_write_is_refused(workspace: Workspace):
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.propose_write("huge.md", "x" * (MAX_WRITE_BYTES + 1))
    assert "limit" in str(excinfo.value)


def test_proposing_a_delete_shows_what_would_be_removed(workspace: Workspace):
    (workspace.root / "old.md").write_text("Delete me.\n", encoding="utf-8")
    proposal = workspace.propose_delete("old.md", note="superseded")
    assert proposal.action == "delete"
    assert "-Delete me." in proposal.diff
    assert (workspace.root / "old.md").exists()


def test_deleting_something_that_does_not_exist_is_an_error(workspace: Workspace):
    with pytest.raises(WorkspaceError):
        workspace.propose_delete("ghost.md")


# --- approving and rejecting ---------------------------------------------


def test_approval_is_what_writes(workspace: Workspace):
    proposal = workspace.propose_write("drafts/plan.md", "# Plan\n")
    result = workspace.approve(proposal.id)

    assert result["applied"] is True
    assert (workspace.root / "drafts" / "plan.md").read_text(encoding="utf-8") == "# Plan\n"
    assert workspace.get(proposal.id).status == "approved"


def test_an_overwrite_keeps_a_backup_of_the_old_version(workspace: Workspace):
    target = workspace.root / "plan.md"
    target.write_text("original\n", encoding="utf-8")

    proposal = workspace.propose_write("plan.md", "replaced\n")
    result = workspace.approve(proposal.id)

    assert target.read_text(encoding="utf-8") == "replaced\n"
    backup = result["backup"]
    assert backup and "original" in open(backup, encoding="utf-8").read()


def test_an_approved_delete_removes_the_file_but_keeps_a_backup(workspace: Workspace):
    target = workspace.root / "old.md"
    target.write_text("gone soon\n", encoding="utf-8")

    proposal = workspace.propose_delete("old.md")
    result = workspace.approve(proposal.id)

    assert not target.exists()
    assert "gone soon" in open(result["backup"], encoding="utf-8").read()


def test_rejecting_leaves_the_file_untouched(workspace: Workspace):
    (workspace.root / "plan.md").write_text("original\n", encoding="utf-8")
    proposal = workspace.propose_write("plan.md", "replaced\n")

    result = workspace.reject(proposal.id, reason="not what I meant")
    assert result["applied"] is False
    assert (workspace.root / "plan.md").read_text(encoding="utf-8") == "original\n"
    assert workspace.get(proposal.id).status == "rejected"


def test_a_proposal_cannot_be_applied_twice(workspace: Workspace):
    proposal = workspace.propose_write("plan.md", "content\n")
    workspace.approve(proposal.id)
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.approve(proposal.id)
    assert "already approved" in str(excinfo.value)


def test_a_rejected_proposal_cannot_then_be_approved(workspace: Workspace):
    proposal = workspace.propose_write("plan.md", "content\n")
    workspace.reject(proposal.id)
    with pytest.raises(WorkspaceError):
        workspace.approve(proposal.id)


def test_an_unknown_proposal_id_is_an_error(workspace: Workspace):
    with pytest.raises(WorkspaceError):
        workspace.approve("wp_does_not_exist")


# --- staleness ------------------------------------------------------------


def test_a_file_changed_after_the_diff_is_not_clobbered(workspace: Workspace):
    target = workspace.root / "plan.md"
    target.write_text("version one\n", encoding="utf-8")
    proposal = workspace.propose_write("plan.md", "version two\n")

    # Something else edits the file after the diff was generated.
    target.write_text("edited by hand\n", encoding="utf-8")

    with pytest.raises(ProposalConflict) as excinfo:
        workspace.approve(proposal.id)
    assert "changed after this diff" in str(excinfo.value)
    assert target.read_text(encoding="utf-8") == "edited by hand\n"
    assert workspace.get(proposal.id).status == "expired"


def test_a_file_created_after_a_create_proposal_is_not_clobbered(workspace: Workspace):
    proposal = workspace.propose_write("plan.md", "from billie\n")
    (workspace.root / "plan.md").write_text("written by hand\n", encoding="utf-8")

    with pytest.raises(ProposalConflict):
        workspace.approve(proposal.id)
    assert (workspace.root / "plan.md").read_text(encoding="utf-8") == "written by hand\n"


def test_proposals_expire(workspace: Workspace):
    workspace.ttl_minutes = 0.0001
    proposal = workspace.propose_write("plan.md", "content\n")
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    workspace.store._conn.execute(  # noqa: SLF001 - forcing the clock in a test
        "UPDATE write_proposals SET expires_at = ? WHERE id = ?",
        (stale.isoformat(timespec="seconds"), proposal.id),
    )
    workspace.store._conn.commit()  # noqa: SLF001

    assert workspace.pending() == []
    assert workspace.get(proposal.id).status == "expired"
    with pytest.raises(WorkspaceError):
        workspace.approve(proposal.id)


# --- listing and audit ----------------------------------------------------


def test_pending_lists_only_unresolved_proposals(workspace: Workspace):
    first = workspace.propose_write("a.md", "a\n")
    second = workspace.propose_write("b.md", "b\n")
    workspace.approve(first.id)

    assert [proposal.id for proposal in workspace.pending()] == [second.id]
    assert len(workspace.history()) == 2


def test_reading_and_listing_workspace_files(workspace: Workspace):
    (workspace.root / "a.md").write_text("hello\n", encoding="utf-8")
    assert workspace.read("a.md") == "hello\n"
    assert [entry["path"] for entry in workspace.list_files()] == ["a.md"]
    with pytest.raises(WorkspaceError):
        workspace.read("missing.md")


def test_every_step_is_audited(workspace: Workspace):
    proposal = workspace.propose_write("plan.md", "content\n")
    workspace.approve(proposal.id)
    rejected = workspace.propose_write("plan.md", "other\n")
    workspace.reject(rejected.id)

    events = [entry["event"] for entry in workspace.audit.tail()]
    assert events == [
        "workspace.proposed",
        "workspace.write",
        "workspace.proposed",
        "workspace.rejected",
    ]
