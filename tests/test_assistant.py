"""The assistant service: retrieve -> route -> answer -> record."""

from __future__ import annotations

import pytest

from billie.assistant import Assistant
from billie.models.base import ProviderUnavailable


async def test_answer_returns_routing_and_grounding_metadata(assistant: Assistant):
    result = await assistant.answer("hi")
    assert result["answer"] == "Local answer."
    assert result["routing"]["kind"] == "local"
    assert result["grounding"]["confidence"] in {"grounded", "unverified", "self-reported-uncertain"}
    assert result["conversation_id"].startswith("cnv_")


async def test_retrieved_notes_are_offered_to_the_model(assistant: Assistant):
    assistant.teach("The deploy command for Billie is `python run.py`.", title="Deploy command")
    result = await assistant.answer("What is the deploy command for Billie?")
    assert result["sources"], "expected the taught fact to be retrieved"
    assert result["sources"][0]["source_type"] == "fact"

    sent = assistant.router.local.calls[-1][-1].content
    assert "[S1]" in sent
    assert "python run.py" in sent


async def test_a_cited_answer_is_reported_as_grounded(assistant: Assistant):
    assistant.teach("Billie's audit log lives in data/audit.log.jsonl.", title="Audit log")
    assistant.router.local.reply = "It lives in data/audit.log.jsonl [S1]."
    result = await assistant.answer("Where is the audit log?")
    assert result["grounding"]["confidence"] == "grounded"
    assert result["grounding"]["citations"][0]["source_id"].startswith("fact:")


async def test_memory_can_be_switched_off_per_request(assistant: Assistant):
    assistant.teach("A fact about deployment.", title="Deployment")
    result = await assistant.answer("Tell me about deployment", use_memory=False)
    assert result["sources"] == []


async def test_conversation_history_is_persisted_and_replayed(assistant: Assistant):
    first = await assistant.answer("hi")
    conversation_id = first["conversation_id"]
    await assistant.answer("and again", conversation_id=conversation_id)

    messages = assistant.store.get_messages(conversation_id)
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    # The second call must have seen the first exchange.
    replayed = assistant.router.local.calls[-1]
    assert len(replayed) == 3
    assert replayed[0].content == "hi"


async def test_every_turn_is_written_to_the_audit_log(assistant: Assistant):
    await assistant.answer("hi")
    events = [entry["event"] for entry in assistant.audit.tail()]
    assert "chat.request" in events and "chat.response" in events
    response_entry = [e for e in assistant.audit.tail() if e["event"] == "chat.response"][-1]
    assert response_entry["detail"]["model"] == "llama3.1:8b"
    assert response_entry["detail"]["kind"] == "local"


async def test_private_requests_are_recorded_as_private_and_stay_local(assistant: Assistant):
    result = await assistant.answer("Something sensitive", private=True)
    assert result["routing"]["kind"] == "local"
    assert result["routing"]["private"] is True
    assert assistant.router.cloud.calls == []
    request_entry = [e for e in assistant.audit.tail() if e["event"] == "chat.request"][-1]
    assert request_entry["detail"]["private"] is True


async def test_an_unavailable_model_raises_rather_than_answering_anyway(assistant: Assistant):
    assistant.router.local.healthy = False
    assistant.router.cloud.healthy = False
    with pytest.raises(ProviderUnavailable):
        await assistant.answer("hi")


async def test_streaming_emits_start_deltas_and_final(assistant: Assistant):
    chunks = [chunk async for chunk in assistant.answer_stream("hi")]
    kinds = [chunk.type for chunk in chunks]
    assert kinds[0] == "start" and kinds[-1] == "final"
    assert "delta" in kinds
    streamed = "".join(c.data["text"] for c in chunks if c.type == "delta").strip()
    assert streamed == chunks[-1].data["answer"]


async def test_reindex_picks_up_the_notes_folder(assistant: Assistant):
    (assistant.settings.notes_dir / "kit.md").write_text(
        "# Kit\n\nThe microphone is a Shure SM7B.", encoding="utf-8"
    )
    report = assistant.reindex_notes()
    assert report.indexed == 1
    results = assistant.search("what microphone do I have")
    assert results and results[0].source_id == "kit.md"


async def test_status_reports_both_providers_and_memory(assistant: Assistant):
    status = await assistant.status()
    assert status["providers"]["cloud"]["model"] == "claude-opus-5"
    assert status["providers"]["local"]["model"] == "llama3.1:8b"
    assert status["memory"]["embedder"].startswith("hashing")
    assert status["routing"]["default_route"] == "auto"
