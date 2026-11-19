"""Putting calendar events into memory, so answers can cite them.

Rather than teaching the model a new tool, upcoming events are indexed as
documents like any note or file. "What have I got on Thursday?" is then
answered by the retrieval and citation machinery that already exists, and the
answer cites `calendar/primary/<event id>` -- so you can see which event it
read.

Events are keyed by their Google id, so a re-sync updates what changed and the
stale sweep removes anything cancelled or moved out of the window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from billie.integrations.google_calendar import CalendarError, GoogleCalendar
from billie.memory.store import MemoryStore

logger = logging.getLogger("billie.calendar.sync")

CALENDAR_SOURCE_TYPE = "calendar"


@dataclass
class SyncReport:
    synced: int = 0
    unchanged: int = 0
    removed: int = 0
    calendars: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "synced": self.synced,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "calendars": self.calendars,
            "errors": self.errors,
        }


async def sync_calendar(
    calendar: GoogleCalendar,
    store: MemoryStore,
    *,
    calendar_ids: list[str] | None = None,
    days_past: int = 1,
    days_ahead: int = 60,
) -> SyncReport:
    """Pull events in the window into memory. Read-only against Google."""
    report = SyncReport()

    if calendar_ids is None:
        try:
            calendars = await calendar.list_calendars()
        except CalendarError as exc:
            report.errors.append(str(exc))
            return report
        calendar_ids = [entry["id"] for entry in calendars if entry.get("id")]

    seen: set[str] = set()
    for calendar_id in calendar_ids:
        try:
            events = await calendar.list_events(
                calendar_id, days_past=days_past, days_ahead=days_ahead
            )
        except CalendarError as exc:
            report.errors.append(f"{calendar_id}: {exc}")
            continue

        report.calendars.append(calendar_id)
        for event in events:
            source_id = f"calendar/{calendar_id}/{event.id}"
            seen.add(source_id)
            _, changed = store.upsert_document(
                source_type=CALENDAR_SOURCE_TYPE,
                source_id=source_id,
                title=event.summary,
                content=event.as_document(),
                metadata={
                    "calendar_id": calendar_id,
                    "event_id": event.id,
                    "start": event.start,
                    "end": event.end,
                    "all_day": event.all_day,
                    "link": event.link,
                },
            )
            if changed:
                report.synced += 1
            else:
                report.unchanged += 1

    # Only sweep when at least one calendar was read, so a transient failure
    # does not wipe every event out of memory.
    if report.calendars:
        for stale in store.document_source_ids(CALENDAR_SOURCE_TYPE) - seen:
            if store.delete_document(stale):
                report.removed += 1
    return report
