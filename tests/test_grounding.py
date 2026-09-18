"""Citations and the confidence flag."""

from __future__ import annotations

from billie.grounding import (
    build_system_prompt,
    build_user_content,
    evaluate,
    format_sources,
)
from billie.memory.store import RetrievedChunk


def chunk(index: int = 1, text: str = "Billie routes private requests locally.") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"chk{index}",
        document_id=f"doc{index}",
        source_type="note",
        source_id=f"note{index}.md",
        title=f"Note {index}",
        text=text,
        ordinal=0,
        score=0.8,
    )


def test_system_prompt_demands_a_source_or_an_admission():
    prompt = build_system_prompt("Ben")
    assert "Ben" in prompt
    assert "not certain" in prompt
    assert "Never invent" in prompt or "never invent" in prompt.lower()


def test_sources_are_rendered_with_citable_tags():
    rendered = format_sources([chunk(1), chunk(2)])
    assert "[S1]" in rendered and "[S2]" in rendered
    assert "note1.md#0" in rendered


def test_no_sources_produces_an_explicit_note():
    rendered = format_sources([], owner="Ben")
    assert "nothing matched" in rendered
    assert "not grounded" in rendered


def test_user_content_carries_both_context_and_question():
    content = build_user_content("What is the routing rule?", [chunk(1)])
    assert "What is the routing rule?" in content
    assert "[S1]" in content


def test_a_cited_answer_is_grounded():
    result = evaluate("Private requests stay on-device [S1].", [chunk(1)])
    assert result.confidence == "grounded"
    assert result.grounded is True
    assert result.citations[0]["source_id"] == "note1.md"


def test_an_uncited_answer_is_flagged_unverified():
    result = evaluate("Private requests stay on-device.", [chunk(1)])
    assert result.confidence == "unverified"
    assert result.grounded is False
    assert "not verified" in result.note


def test_a_self_reported_uncertainty_is_recorded_as_such():
    result = evaluate("I'm not certain, and I don't have a source for this.", [chunk(1)])
    assert result.confidence == "self-reported-uncertain"
    assert result.grounded is False


def test_a_citation_to_a_source_that_was_never_offered_is_not_grounded():
    result = evaluate("According to [S7] the answer is 42.", [chunk(1)])
    assert result.confidence == "unverified"
    assert result.grounded is False
    assert "S7" in result.note


def test_answers_with_no_sources_offered_are_unverified():
    result = evaluate("The capital of France is Paris.", [])
    assert result.sources_offered == 0
    assert result.grounded is False
