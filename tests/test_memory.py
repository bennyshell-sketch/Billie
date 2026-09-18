"""Embeddings, the store, retrieval, and the notes indexer."""

from __future__ import annotations

import numpy as np
import pytest

from billie.memory.embeddings import HashingEmbedder
from billie.memory.notes import index_notes
from billie.memory.store import MemoryStore, chunk_text


def test_hashing_embedder_is_deterministic_and_normalised():
    embedder = HashingEmbedder(dim=128)
    first = embedder.embed(["the cat sat on the mat"])
    second = embedder.embed(["the cat sat on the mat"])
    assert np.allclose(first, second)
    assert pytest.approx(1.0, abs=1e-5) == float(np.linalg.norm(first[0]))


def test_similar_text_scores_higher_than_unrelated_text():
    embedder = HashingEmbedder(dim=512)
    query = embedder.embed_one("how do I deploy the billie backend")
    related = embedder.embed_one("deploying the billie backend needs uvicorn")
    unrelated = embedder.embed_one("recipe for sourdough bread starter")
    assert float(query @ related) > float(query @ unrelated)


def test_chunk_text_splits_long_content_with_overlap():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(6))
    chunks = chunk_text(text, target=400, overlap=80)
    assert len(chunks) > 1
    assert all(chunk.strip() for chunk in chunks)
    assert chunk_text("") == []


def test_upsert_is_idempotent_for_unchanged_content(store: MemoryStore):
    first_id, changed = store.upsert_document(
        source_type="note", source_id="a.md", title="A", content="Hello world."
    )
    assert changed is True
    second_id, changed_again = store.upsert_document(
        source_type="note", source_id="a.md", title="A", content="Hello world."
    )
    assert second_id == first_id
    assert changed_again is False
    assert store.stats()["documents"] == 1


def test_updating_content_replaces_the_old_chunks(store: MemoryStore):
    store.upsert_document(
        source_type="note", source_id="a.md", title="A", content="Original content here."
    )
    chunks_before = store.stats()["chunks"]
    store.upsert_document(
        source_type="note",
        source_id="a.md",
        title="A",
        content="\n\n".join(["Completely different." * 20] * 4),
    )
    assert store.stats()["documents"] == 1
    assert store.stats()["chunks"] != chunks_before
    results = store.search("Original content")
    assert all("Original content" not in result.text for result in results)


def test_search_returns_the_relevant_document_first(store: MemoryStore):
    store.upsert_document(
        source_type="note",
        source_id="ollama.md",
        title="Ollama setup",
        content="Run ollama serve and then pull llama3.1:8b to use the local model.",
    )
    store.upsert_document(
        source_type="note",
        source_id="cake.md",
        title="Victoria sponge",
        content="Cream the butter and sugar, fold in flour, bake for 25 minutes.",
    )
    results = store.search("how do I pull the local llama model", top_k=2)
    assert results
    assert results[0].source_id == "ollama.md"
    assert results[0].citation.startswith("ollama.md#")


def test_search_respects_min_score_and_empty_queries(store: MemoryStore):
    store.upsert_document(
        source_type="note", source_id="a.md", title="A", content="Something entirely unrelated."
    )
    assert store.search("", top_k=3) == []
    assert store.search("zygomorphic quantum basilisk", top_k=3, min_score=0.9) == []


def test_conversations_persist_messages_with_metadata(store: MemoryStore):
    conversation_id = store.create_conversation("Test")
    store.add_message(conversation_id, "user", "hi")
    store.add_message(conversation_id, "assistant", "hello", {"routing": {"kind": "local"}})
    messages = store.get_messages(conversation_id)
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["metadata"]["routing"]["kind"] == "local"
    assert store.list_conversations()[0]["message_count"] == 2


def test_index_notes_adds_updates_and_removes(store: MemoryStore, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir(exist_ok=True)
    (notes / "one.md").write_text("# One\n\nThe first note.", encoding="utf-8")
    (notes / "two.txt").write_text("The second note.", encoding="utf-8")
    (notes / "ignored.pdf").write_bytes(b"%PDF-1.4 binary")

    report = index_notes(store, notes)
    assert report.indexed == 2
    assert {doc["source_id"] for doc in store.list_documents("note")} == {"one.md", "two.txt"}
    assert store.list_documents("note")[0]["title"] in {"One", "two"}

    again = index_notes(store, notes)
    assert again.indexed == 0 and again.unchanged == 2

    (notes / "one.md").unlink()
    after_delete = index_notes(store, notes)
    assert after_delete.removed == 1
    assert store.document_source_ids("note") == {"two.txt"}


def test_index_notes_on_missing_folder_is_harmless(store: MemoryStore, tmp_path):
    report = index_notes(store, tmp_path / "does-not-exist")
    assert report.indexed == 0 and report.removed == 0
