"""Grounding, citations, and the confidence flag.

This module does three things:

1. Builds the system prompt that tells Billie to cite a source for every
   factual claim, or say plainly that it is not certain.
2. Formats retrieved memory into numbered source blocks the model can cite.
3. Reads the answer back and works out whether it was actually grounded --
   which is what the UI's confidence flag reports.

This is an honest signal, not a guarantee: it shows whether the answer cited
retrieved material, so an unverified answer is visibly unverified. It cannot
prove a cited answer is true.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from billie.memory.store import RetrievedChunk

Confidence = Literal["grounded", "self-reported-uncertain", "unverified"]

CITATION_RE = re.compile(r"\[S(\d+)\]")

_UNCERTAINTY_RE = re.compile(
    r"(i'?m not certain|i am not certain|i don'?t have a source|"
    r"i do not have a source|no source for this|i'?m not sure|"
    r"i am not sure|i don'?t know|i do not know|from memory, not a source|"
    r"without a source|unverified)",
    re.IGNORECASE,
)

BASE_SYSTEM_PROMPT = """You are Billie, {owner}'s personal assistant. You run \
locally on {owner}'s own machine and you answer only to them.

Accuracy rules, which override everything else:

1. For any factual claim, either cite the source you took it from using its \
tag -- for example [S1] -- or say plainly that you are not certain and have no \
source for it. Never present a guess as a fact.
2. Only cite the sources listed under "Retrieved from memory" below. Never \
invent a source tag, a filename, a URL, a quotation, a statistic, or a date.
3. If the retrieved sources do not answer the question, say so directly, then \
answer from general knowledge only if that is useful -- and label that part as \
not having a source.
4. When you are reasoning, drafting, or brainstorming rather than stating \
facts, no citation is needed. Say when you are doing that.
5. If asked about something current, specific, or personal that you have no \
retrieved source for, say "I don't have a source for this" rather than \
guessing. Being useless is better than being confidently wrong.

Style: direct and concise. No filler, no flattery. Plain prose."""

NO_SOURCES_NOTE = """Retrieved from memory: nothing matched this request.

You have no retrieved sources for this turn. Answer from general knowledge if \
you can, and say explicitly that the answer is not grounded in {owner}'s notes."""


@dataclass
class GroundingResult:
    """What the confidence flag in the UI is built from."""

    confidence: Confidence
    grounded: bool
    citations: list[dict[str, Any]] = field(default_factory=list)
    sources_offered: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "grounded": self.grounded,
            "citations": self.citations,
            "sources_offered": self.sources_offered,
            "note": self.note,
        }


def build_system_prompt(owner: str = "the user", extra: str | None = None) -> str:
    prompt = BASE_SYSTEM_PROMPT.format(owner=owner)
    if extra:
        prompt = f"{prompt}\n\n{extra.strip()}"
    return prompt


def format_sources(chunks: list[RetrievedChunk], owner: str = "the user") -> str:
    """Render retrieved chunks as numbered, citable source blocks."""
    if not chunks:
        return NO_SOURCES_NOTE.format(owner=owner)
    blocks = ["Retrieved from memory. Cite these by tag when you use them:\n"]
    for index, chunk in enumerate(chunks, start=1):
        blocks.append(
            f"[S{index}] {chunk.title} ({chunk.citation}, relevance {chunk.score:.2f})\n"
            f"{chunk.text.strip()}\n"
        )
    return "\n".join(blocks)


def build_user_content(question: str, chunks: list[RetrievedChunk], owner: str = "the user") -> str:
    """Combine the question with its retrieved context."""
    return f"{format_sources(chunks, owner)}\n---\n\n{question.strip()}"


def evaluate(answer: str, chunks: list[RetrievedChunk]) -> GroundingResult:
    """Decide the confidence flag for a finished answer."""
    offered = len(chunks)
    cited_indexes = sorted({int(match) for match in CITATION_RE.findall(answer)})
    valid = [index for index in cited_indexes if 1 <= index <= offered]
    hallucinated = [index for index in cited_indexes if index not in valid]

    citations = [
        {
            "tag": f"S{index}",
            "title": chunks[index - 1].title,
            "source_id": chunks[index - 1].source_id,
            "citation": chunks[index - 1].citation,
            "score": round(chunks[index - 1].score, 4),
        }
        for index in valid
    ]

    if hallucinated:
        return GroundingResult(
            confidence="unverified",
            grounded=False,
            citations=citations,
            sources_offered=offered,
            note=(
                "The answer cited source tags that were not provided "
                f"({', '.join('S' + str(index) for index in hallucinated)}). Treat it as unverified."
            ),
        )
    if valid:
        return GroundingResult(
            confidence="grounded",
            grounded=True,
            citations=citations,
            sources_offered=offered,
            note="Grounded in retrieved sources.",
        )
    if _UNCERTAINTY_RE.search(answer):
        return GroundingResult(
            confidence="self-reported-uncertain",
            grounded=False,
            sources_offered=offered,
            note="Billie said it was not certain and gave no source.",
        )
    return GroundingResult(
        confidence="unverified",
        grounded=False,
        sources_offered=offered,
        note=(
            "No source was cited, so this answer comes from the model's own "
            "knowledge and is not verified against your notes."
        ),
    )
