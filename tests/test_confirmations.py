"""The confirmation queue: nothing external happens without a yes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from billie.confirmations import ConfirmationError, ConfirmationQueue


@pytest.fixture
def queue(confirmations: ConfirmationQueue):
    confirmations.performed = []

    async def executor(payload):
        confirmations.performed.append(payload)
        return {"done": True, **payload}

    confirmations.register("test.action", executor)
    return confirmations


def test_proposing_records_but_does_not_execute(queue):
    action = queue.propose(
        "test.action", summary="Do the thing", preview="It will do the thing", payload={"x": 1}
    )
    assert action.status == "pending"
    assert queue.performed == [], "proposing must not execute"
    assert [pending.id for pending in queue.pending()] == [action.id]


def test_an_unregistered_kind_cannot_be_proposed(queue):
    with pytest.raises(ConfirmationError):
        queue.propose("test.unknown", summary="s", preview="p", payload={})


async def test_approval_executes_once_and_records_the_result(queue):
    action = queue.propose("test.action", summary="s", preview="p", payload={"x": 1})
    result = await queue.approve(action.id)

    assert result["applied"] is True
    assert result["result"]["done"] is True
    assert queue.performed == [{"x": 1}]
    assert queue.get(action.id).status == "approved"
    assert queue.pending() == []


async def test_an_action_cannot_be_approved_twice(queue):
    action = queue.propose("test.action", summary="s", preview="p", payload={})
    await queue.approve(action.id)
    with pytest.raises(ConfirmationError) as excinfo:
        await queue.approve(action.id)
    assert "already approved" in str(excinfo.value)
    assert len(queue.performed) == 1


async def test_rejecting_never_executes(queue):
    action = queue.propose("test.action", summary="s", preview="p", payload={})
    queue.reject(action.id, reason="changed my mind")

    assert queue.performed == []
    assert queue.get(action.id).status == "rejected"
    with pytest.raises(ConfirmationError):
        await queue.approve(action.id)


async def test_a_failing_executor_marks_the_action_failed_and_raises(queue):
    async def boom(payload):
        raise RuntimeError("Google said no")

    queue.register("test.explodes", boom)
    action = queue.propose("test.explodes", summary="s", preview="p", payload={})

    with pytest.raises(RuntimeError):
        await queue.approve(action.id)
    assert queue.get(action.id).status == "failed"
    assert "Google said no" in queue.get(action.id).resolution


async def test_an_expired_action_cannot_be_approved(queue):
    action = queue.propose("test.action", summary="s", preview="p", payload={})
    stale = datetime.now(timezone.utc) - timedelta(hours=3)
    queue.store._conn.execute(  # noqa: SLF001 - forcing the clock in a test
        "UPDATE pending_actions SET expires_at = ? WHERE id = ?",
        (stale.isoformat(timespec="seconds"), action.id),
    )
    queue.store._conn.commit()  # noqa: SLF001

    assert queue.pending() == []
    with pytest.raises(ConfirmationError):
        await queue.approve(action.id)
    assert queue.performed == []


def test_pending_can_be_filtered_by_kind(queue):
    async def other(payload):
        return {}

    queue.register("calendar.create", other)
    queue.propose("test.action", summary="a", preview="p", payload={})
    calendar_action = queue.propose("calendar.create", summary="b", preview="p", payload={})

    assert [action.id for action in queue.pending("calendar.")] == [calendar_action.id]
    assert len(queue.pending()) == 2


async def test_every_step_is_audited(queue):
    approved = queue.propose("test.action", summary="s", preview="p", payload={})
    await queue.approve(approved.id)
    rejected = queue.propose("test.action", summary="s", preview="p", payload={})
    queue.reject(rejected.id)

    events = [entry["event"] for entry in queue.audit.tail()]
    assert events == [
        "confirmation.proposed",
        "confirmation.approved",
        "confirmation.proposed",
        "confirmation.rejected",
    ]


async def test_an_unknown_action_id_is_an_error(queue):
    with pytest.raises(ConfirmationError):
        await queue.approve("pa_nope")
    with pytest.raises(ConfirmationError):
        queue.reject("pa_nope")
