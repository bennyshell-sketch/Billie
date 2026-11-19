"""The background re-indexer."""

from __future__ import annotations

from billie.permissions import FILE_READ
from billie.watcher import FileWatcher


def test_the_watcher_is_off_when_the_interval_is_zero(assistant):
    assert FileWatcher(assistant, 0).enabled is False
    assert FileWatcher(assistant, 30).enabled is True


def test_the_first_pass_does_not_re_index(assistant):
    """Startup already indexed; the first snapshot is just a baseline."""
    (assistant.settings.notes_dir / "a.md").write_text("first", encoding="utf-8")
    watcher = FileWatcher(assistant, 30)
    assert watcher.check_once() is None


def test_a_new_file_triggers_a_re_index(assistant):
    watcher = FileWatcher(assistant, 30)
    watcher.check_once()  # baseline

    (assistant.settings.notes_dir / "new.md").write_text(
        "# New\n\nThe fire drill is on Tuesday.", encoding="utf-8"
    )
    report = watcher.check_once()
    assert report is not None and report.indexed == 1
    assert assistant.search("when is the fire drill")


def test_an_unchanged_folder_does_not_re_index(assistant):
    (assistant.settings.notes_dir / "a.md").write_text("stable", encoding="utf-8")
    watcher = FileWatcher(assistant, 30)
    watcher.check_once()
    assert watcher.check_once() is None


def test_a_deleted_file_triggers_a_re_index(assistant):
    path = assistant.settings.notes_dir / "temp.md"
    path.write_text("here for now", encoding="utf-8")
    assistant.reindex_files()

    watcher = FileWatcher(assistant, 30)
    watcher.check_once()
    path.unlink()

    report = watcher.check_once()
    assert report is not None and report.removed == 1


def test_the_watcher_stays_quiet_when_file_read_is_revoked(assistant):
    watcher = FileWatcher(assistant, 30)
    watcher.check_once()
    assistant.permissions.set(FILE_READ, False)

    (assistant.settings.notes_dir / "new.md").write_text("new", encoding="utf-8")
    assert watcher.check_once() is None


def test_a_re_index_is_recorded_in_the_audit_log(assistant):
    watcher = FileWatcher(assistant, 30)
    watcher.check_once()
    (assistant.settings.notes_dir / "new.md").write_text("new", encoding="utf-8")
    watcher.check_once()

    assert any(entry["event"] == "watcher.reindex" for entry in assistant.audit.tail())
