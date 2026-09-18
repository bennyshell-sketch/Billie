"""Reading the calendar, and the propose/approve path for changing it."""

from __future__ import annotations

import pytest

from billie.confirmations import ConfirmationQueue
from billie.integrations.calendar_sync import sync_calendar
from billie.integrations.google_calendar import CalendarError, Event, GoogleCalendar


# --- formatting -----------------------------------------------------------


def test_an_event_reads_as_a_sentence():
    event = Event(
        id="e1",
        calendar_id="primary",
        summary="Grant meeting",
        start="2027-03-14T09:00:00+00:00",
        end="2027-03-14T10:00:00+00:00",
        all_day=False,
        location="Room 4",
        attendees=["ade@example.com"],
    )
    described = event.describe()
    assert "Grant meeting" in described
    assert "09:00" in described and "10:00" in described
    assert "at Room 4" in described
    assert "with ade@example.com" in described


def test_an_all_day_event_is_described_as_such():
    event = Event("e", "primary", "Leave", "2027-08-01", "2027-08-08", True)
    assert "all day" in event.describe()


# --- reads ----------------------------------------------------------------


async def test_calendars_are_listed(calendar: GoogleCalendar):
    calendars = await calendar.list_calendars()
    assert calendars[0]["id"] == "primary"
    assert calendars[0]["primary"] is True


async def test_events_are_listed_and_parsed(calendar: GoogleCalendar):
    events = await calendar.list_events()
    assert [event.summary for event in events] == ["Team standup"]
    assert events[0].location == "Zoom"
    assert events[0].all_day is False


async def test_a_google_error_becomes_a_readable_message(calendar: GoogleCalendar, google):
    google.fail_next_with = (403, "Insufficient permission")
    with pytest.raises(CalendarError) as excinfo:
        await calendar.list_events()
    assert "reconnect" in str(excinfo.value)


async def test_a_missing_event_says_so(calendar: GoogleCalendar):
    with pytest.raises(CalendarError) as excinfo:
        await calendar.get_event("evt_missing")
    assert "no longer exists" in str(excinfo.value)


async def test_every_api_call_is_audited(calendar: GoogleCalendar, audit):
    await calendar.list_events()
    entry = [e for e in audit.tail() if e["event"] == "google.api_call"][-1]
    assert entry["detail"]["method"] == "GET"
    assert entry["detail"]["status"] == 200


# --- creating -------------------------------------------------------------


async def test_proposing_an_event_sends_nothing_to_google(calendar: GoogleCalendar, google):
    before = len(google.events)
    action = await calendar.propose_create(
        "Dentist", "2027-04-01T09:00:00+00:00", "2027-04-01T09:30:00+00:00"
    )

    assert len(google.events) == before, "proposing must not create anything"
    assert action.status == "pending"
    assert "CREATE" in action.preview
    assert "Dentist" in action.summary


async def test_the_preview_warns_when_invitations_will_be_sent(calendar: GoogleCalendar):
    action = await calendar.propose_create(
        "Review",
        "2027-04-01T09:00:00+00:00",
        "2027-04-01T10:00:00+00:00",
        attendees=["ade@example.com"],
    )
    assert "Invitations will be emailed to: ade@example.com" in action.preview


async def test_an_event_without_a_title_is_refused(calendar: GoogleCalendar):
    with pytest.raises(CalendarError):
        await calendar.propose_create("  ", "2027-04-01", "2027-04-02")


async def test_approving_creates_the_event(
    calendar: GoogleCalendar, confirmations: ConfirmationQueue, google
):
    action = await calendar.propose_create(
        "Dentist", "2027-04-01T09:00:00+00:00", "2027-04-01T09:30:00+00:00", location="Clinic"
    )
    result = await confirmations.approve(action.id)

    created = google.events[result["result"]["event_id"]]
    assert created["summary"] == "Dentist"
    assert created["location"] == "Clinic"
    assert created["start"]["dateTime"] == "2027-04-01T09:00:00+00:00"


async def test_an_all_day_event_is_sent_as_a_date_not_a_datetime(
    calendar: GoogleCalendar, confirmations: ConfirmationQueue, google
):
    action = await calendar.propose_create(
        "Leave", "2027-08-01", "2027-08-08", all_day=True
    )
    result = await confirmations.approve(action.id)

    created = google.events[result["result"]["event_id"]]
    assert created["start"] == {"date": "2027-08-01"}
    assert "dateTime" not in created["start"]


async def test_rejecting_a_creation_leaves_the_calendar_alone(
    calendar: GoogleCalendar, confirmations: ConfirmationQueue, google
):
    before = dict(google.events)
    action = await calendar.propose_create(
        "Dentist", "2027-04-01T09:00:00+00:00", "2027-04-01T09:30:00+00:00"
    )
    confirmations.reject(action.id)
    assert google.events == before


# --- moving ---------------------------------------------------------------


async def test_a_move_preview_shows_both_times(calendar: GoogleCalendar, google):
    action = await calendar.propose_move(
        "evt_standup", "2027-03-14T11:00:00+00:00", "2027-03-14T11:15:00+00:00"
    )
    assert "from:" in action.preview and "to:" in action.preview
    assert "09:00" in action.preview and "11:00" in action.preview
    assert google.events["evt_standup"]["start"]["dateTime"] == "2027-03-14T09:00:00+00:00"


async def test_approving_a_move_updates_the_event(
    calendar: GoogleCalendar, confirmations: ConfirmationQueue, google
):
    action = await calendar.propose_move(
        "evt_standup", "2027-03-14T11:00:00+00:00", "2027-03-14T11:15:00+00:00"
    )
    await confirmations.approve(action.id)
    assert google.events["evt_standup"]["start"]["dateTime"] == "2027-03-14T11:00:00+00:00"


async def test_moving_an_event_that_does_not_exist_fails_at_proposal_time(
    calendar: GoogleCalendar,
):
    with pytest.raises(CalendarError):
        await calendar.propose_move("evt_nope", "2027-03-14T11:00:00+00:00", "2027-03-14T11:15:00+00:00")


# --- deleting -------------------------------------------------------------


async def test_a_delete_preview_names_the_event_and_warns(calendar: GoogleCalendar, google):
    action = await calendar.propose_delete("evt_standup")
    assert "DELETE" in action.preview
    assert "Team standup" in action.preview
    assert "cannot be undone" in action.preview
    assert "evt_standup" in google.events, "proposing must not delete"


async def test_approving_a_delete_removes_the_event(
    calendar: GoogleCalendar, confirmations: ConfirmationQueue, google
):
    action = await calendar.propose_delete("evt_standup")
    await confirmations.approve(action.id)
    assert "evt_standup" not in google.events


# --- syncing into memory --------------------------------------------------


async def test_events_are_indexed_and_citable(calendar: GoogleCalendar, store):
    report = await sync_calendar(calendar, store)
    assert report.synced == 1
    assert report.calendars == ["primary"]

    results = store.search("when is the team standup")
    assert results
    assert results[0].source_id == "calendar/primary/evt_standup"
    assert "Zoom" in results[0].text


async def test_resyncing_unchanged_events_is_a_no_op(calendar: GoogleCalendar, store):
    await sync_calendar(calendar, store)
    second = await sync_calendar(calendar, store)
    assert second.synced == 0 and second.unchanged == 1


async def test_a_cancelled_event_drops_out_of_memory(calendar: GoogleCalendar, store, google):
    await sync_calendar(calendar, store)
    del google.events["evt_standup"]

    report = await sync_calendar(calendar, store)
    assert report.removed == 1
    assert store.document_source_ids("calendar") == set()


async def test_a_failed_sync_does_not_wipe_memory(calendar: GoogleCalendar, store, google):
    await sync_calendar(calendar, store)
    google.fail_next_with = (500, "Google is having a day")

    report = await sync_calendar(calendar, store)
    assert report.errors
    assert report.removed == 0
    assert store.document_source_ids("calendar") == {"calendar/primary/evt_standup"}
