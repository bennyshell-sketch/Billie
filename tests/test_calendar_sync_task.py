"""The background calendar sync."""

from __future__ import annotations

from billie.permissions import CALENDAR_READ
from billie.watcher import CalendarSyncTask


def test_it_is_off_without_an_interval_or_a_calendar(assistant):
    assert CalendarSyncTask(assistant, 0).enabled is False
    assert CalendarSyncTask(assistant, 900).enabled is True

    assistant.calendar = None
    assert CalendarSyncTask(assistant, 900).enabled is False


async def test_it_pulls_events_into_memory(assistant):
    report = await CalendarSyncTask(assistant, 900).check_once()
    assert report is not None and report.synced == 1
    assert assistant.search("team standup")


async def test_it_stays_quiet_when_calendar_read_is_revoked(assistant):
    assistant.permissions.set(CALENDAR_READ, False)
    assert await CalendarSyncTask(assistant, 900).check_once() is None


async def test_it_stays_quiet_when_no_account_is_connected(assistant):
    oauth = assistant.calendar.oauth
    oauth.token_store.delete(oauth.account)
    assert await CalendarSyncTask(assistant, 900).check_once() is None


async def test_it_stays_quiet_when_google_is_not_configured(assistant):
    assistant.calendar = None
    assert await CalendarSyncTask(assistant, 900).check_once() is None


async def test_a_sync_is_audited(assistant):
    await CalendarSyncTask(assistant, 900).check_once()
    assert any(entry["event"] == "calendar.sync" for entry in assistant.audit.tail())
