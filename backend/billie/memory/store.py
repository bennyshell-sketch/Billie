"""SQLite-backed memory store with vector retrieval.

Everything lives in one local SQLite file: documents (notes, taught facts,
conversation summaries), their chunks, the vectors for those chunks, and the
conversation history itself.

Retrieval is a hybrid of cosine similarity over the chunk vectors and a
lexical overlap score. For a single user's notes the whole vector set fits in
memory comfortably, so the search is an exact numpy dot product rather than an
approximate index -- simpler, and exact.

Phase 8 of the roadmap swaps the plain SQLite file for an encrypted one
(SQLCipher); the schema and this API do not change when that happens.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from billie.memory.embeddings import Embedder, content_tokens

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id            TEXT PRIMARY KEY,
    source_type   TEXT NOT NULL,          -- note | fact | conversation
    source_id     TEXT NOT NULL UNIQUE,   -- file path, or a generated id
    title         TEXT NOT NULL,
    content       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    metadata      TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id            TEXT PRIMARY KEY,
    document_id   TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal       INTEGER NOT NULL,
    text          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id      TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    embedder      TEXT NOT NULL,
    dim           INTEGER NOT NULL,
    vector        BLOB NOT NULL,
    PRIMARY KEY (chunk_id, embedder)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_embedder ON embeddings(embedder);

CREATE TABLE IF NOT EXISTS conversations (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);

CREATE TABLE IF NOT EXISTS permissions (
    tool       TEXT PRIMARY KEY,
    granted    INTEGER NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS write_proposals (
    id           TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    action       TEXT NOT NULL,          -- create | update | delete
    new_content  TEXT,
    diff         TEXT NOT NULL,
    base_hash    TEXT NOT NULL,          -- '' when the file does not exist yet
    note         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,          -- pending | approved | rejected | expired
    resolution   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    resolved_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON write_proposals(status);

CREATE TABLE IF NOT EXISTS pending_actions (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,          -- calendar.create | calendar.move | ...
    summary      TEXT NOT NULL,          -- one line: what will happen
    preview      TEXT NOT NULL,          -- the full before/after, for display
    payload      TEXT NOT NULL,          -- JSON the executor needs
    status       TEXT NOT NULL,          -- pending | approved | rejected | expired | failed
    resolution   TEXT NOT NULL DEFAULT '',
    result       TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    resolved_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON pending_actions(status);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_text(text: str, *, target: int = 900, overlap: int = 150) -> list[str]:
    """Split text into overlapping chunks, preferring paragraph boundaries."""
    text = text.strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > target:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = (tail + "\n\n" + paragraph).strip() if tail else paragraph
        else:
            current = f"{current}\n\n{paragraph}".strip() if current else paragraph
    if current:
        chunks.append(current)

    # A single paragraph longer than the target still needs splitting.
    final: list[str] = []
    for chunk in chunks:
        if len(chunk) <= target * 2:
            final.append(chunk)
            continue
        step = target - overlap
        for start in range(0, len(chunk), step):
            piece = chunk[start : start + target]
            if piece.strip():
                final.append(piece.strip())
    return final


@dataclass
class RetrievedChunk:
    """One retrieved piece of memory, with everything needed to cite it."""

    chunk_id: str
    document_id: str
    source_type: str
    source_id: str
    title: str
    text: str
    ordinal: int
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def citation(self) -> str:
        return f"{self.source_id}#{self.ordinal}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "title": self.title,
            "text": self.text,
            "ordinal": self.ordinal,
            "score": round(self.score, 4),
            "citation": self.citation,
            "metadata": self.metadata,
        }


class MemoryStore:
    """All local persistence for Billie."""

    def __init__(self, db_path: Path | str, embedder: Embedder) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------
    def upsert_document(
        self,
        *,
        source_type: str,
        source_id: str,
        title: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """Insert or update a document. Returns `(document_id, changed)`.

        Unchanged content is a no-op, so re-indexing a notes folder is cheap.
        """
        digest = content_hash(content)
        metadata = metadata or {}
        with self._lock:
            row = self._conn.execute(
                "SELECT id, content_hash FROM documents WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            now = _now()
            if row and row["content_hash"] == digest:
                self._conn.execute(
                    "UPDATE documents SET title = ?, metadata = ?, updated_at = ? WHERE id = ?",
                    (title, json.dumps(metadata), now, row["id"]),
                )
                self._conn.commit()
                return row["id"], False

            if row:
                document_id = row["id"]
                self._conn.execute(
                    """UPDATE documents
                       SET title = ?, content = ?, content_hash = ?, metadata = ?, updated_at = ?
                       WHERE id = ?""",
                    (title, content, digest, json.dumps(metadata), now, document_id),
                )
                self._conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            else:
                document_id = _new_id("doc")
                self._conn.execute(
                    """INSERT INTO documents
                       (id, source_type, source_id, title, content, content_hash,
                        metadata, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        document_id,
                        source_type,
                        source_id,
                        title,
                        content,
                        digest,
                        json.dumps(metadata),
                        now,
                        now,
                    ),
                )
            self._conn.commit()

        self._index_document(document_id, content)
        return document_id, True

    def _index_document(self, document_id: str, content: str) -> None:
        pieces = chunk_text(content)
        if not pieces:
            return
        vectors = self.embedder.embed(pieces)
        rows = []
        vector_rows = []
        for ordinal, (piece, vector) in enumerate(zip(pieces, vectors)):
            chunk_id = _new_id("chk")
            rows.append((chunk_id, document_id, ordinal, piece))
            vector_rows.append(
                (
                    chunk_id,
                    self.embedder.name,
                    int(vector.shape[0]),
                    np.asarray(vector, dtype=np.float32).tobytes(),
                )
            )
        with self._lock:
            self._conn.executemany(
                "INSERT INTO chunks (id, document_id, ordinal, text) VALUES (?, ?, ?, ?)",
                rows,
            )
            self._conn.executemany(
                "INSERT OR REPLACE INTO embeddings (chunk_id, embedder, dim, vector)"
                " VALUES (?, ?, ?, ?)",
                vector_rows,
            )
            self._conn.commit()

    def delete_document(self, source_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM documents WHERE source_id = ?", (source_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def list_documents(self, source_type: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT id, source_type, source_id, title, content_hash, metadata,"
            " created_at, updated_at, length(content) AS size FROM documents"
        )
        params: tuple[Any, ...] = ()
        if source_type:
            query += " WHERE source_type = ?"
            params = (source_type,)
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {**dict(row), "metadata": json.loads(row["metadata"] or "{}")} for row in rows
        ]

    def document_source_ids(self, source_type: str | None = None) -> set[str]:
        return {doc["source_id"] for doc in self.list_documents(source_type)}

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
        source_types: Iterable[str] | None = None,
    ) -> list[RetrievedChunk]:
        """Hybrid vector + lexical search over all indexed chunks."""
        query = query.strip()
        if not query:
            return []

        sql = """
            SELECT e.chunk_id, e.vector, e.dim, c.document_id, c.ordinal, c.text,
                   d.source_type, d.source_id, d.title, d.metadata
            FROM embeddings e
            JOIN chunks c ON c.id = e.chunk_id
            JOIN documents d ON d.id = c.document_id
            WHERE e.embedder = ?
        """
        params: list[Any] = [self.embedder.name]
        types = list(source_types) if source_types else []
        if types:
            sql += f" AND d.source_type IN ({','.join('?' * len(types))})"
            params.extend(types)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        if not rows:
            return []

        matrix = np.stack(
            [np.frombuffer(row["vector"], dtype=np.float32) for row in rows]
        )
        query_vector = self.embedder.embed_one(query)
        similarities = matrix @ query_vector

        query_tokens = set(content_tokens(query))
        results: list[RetrievedChunk] = []
        for row, similarity in zip(rows, similarities):
            lexical = 0.0
            if query_tokens:
                chunk_tokens = set(content_tokens(row["text"]))
                lexical = len(query_tokens & chunk_tokens) / len(query_tokens)
            score = 0.75 * float(similarity) + 0.25 * lexical
            if score < min_score:
                continue
            results.append(
                RetrievedChunk(
                    chunk_id=row["chunk_id"],
                    document_id=row["document_id"],
                    source_type=row["source_type"],
                    source_id=row["source_id"],
                    title=row["title"],
                    text=row["text"],
                    ordinal=row["ordinal"],
                    score=score,
                    metadata=json.loads(row["metadata"] or "{}"),
                )
            )
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:top_k]

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------
    def create_conversation(self, title: str = "New conversation") -> str:
        conversation_id = _new_id("cnv")
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (conversation_id, title, now, now),
            )
            self._conn.commit()
        return conversation_id

    def ensure_conversation(self, conversation_id: str | None, title: str) -> str:
        if conversation_id:
            with self._lock:
                row = self._conn.execute(
                    "SELECT id FROM conversations WHERE id = ?", (conversation_id,)
                ).fetchone()
            if row:
                return conversation_id
        return self.create_conversation(title[:80] or "New conversation")

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        message_id = _new_id("msg")
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO messages (id, conversation_id, role, content, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    conversation_id,
                    role,
                    content,
                    json.dumps(metadata or {}),
                    now,
                ),
            )
            self._conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            self._conn.commit()
        return message_id

    def get_messages(self, conversation_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, role, content, metadata, created_at FROM messages
                   WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC
                   LIMIT ?""",
                (conversation_id, limit),
            ).fetchall()
        return [
            {**dict(row), "metadata": json.loads(row["metadata"] or "{}")} for row in rows
        ]

    def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.id, c.title, c.created_at, c.updated_at,
                          (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id)
                          AS message_count
                   FROM conversations c ORDER BY c.updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------
    def get_permission(self, tool: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT tool, granted, note, updated_at FROM permissions WHERE tool = ?",
                (tool,),
            ).fetchone()
        if row is None:
            return None
        return {**dict(row), "granted": bool(row["granted"])}

    def set_permission(self, tool: str, granted: bool, note: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO permissions (tool, granted, note, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(tool) DO UPDATE SET
                       granted = excluded.granted,
                       note = excluded.note,
                       updated_at = excluded.updated_at""",
                (tool, int(granted), note, _now()),
            )
            self._conn.commit()

    def all_permissions(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tool, granted, note, updated_at FROM permissions"
            ).fetchall()
        return {
            row["tool"]: {**dict(row), "granted": bool(row["granted"])} for row in rows
        }

    # ------------------------------------------------------------------
    # Write proposals
    # ------------------------------------------------------------------
    def create_proposal(self, proposal: dict[str, Any]) -> str:
        with self._lock:
            self._conn.execute(
                """INSERT INTO write_proposals
                   (id, path, action, new_content, diff, base_hash, note, status,
                    resolution, created_at, expires_at, resolved_at)
                   VALUES (:id, :path, :action, :new_content, :diff, :base_hash,
                           :note, :status, '', :created_at, :expires_at, NULL)""",
                proposal,
            )
            self._conn.commit()
        return proposal["id"]

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM write_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_proposals(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM write_proposals"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY created_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (*params, limit)).fetchall()
        return [dict(row) for row in rows]

    def resolve_proposal(self, proposal_id: str, status: str, resolution: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE write_proposals
                   SET status = ?, resolution = ?, resolved_at = ?
                   WHERE id = ?""",
                (status, resolution, _now(), proposal_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Pending actions awaiting confirmation
    # ------------------------------------------------------------------
    def create_action(self, action: dict[str, Any]) -> str:
        with self._lock:
            self._conn.execute(
                """INSERT INTO pending_actions
                   (id, kind, summary, preview, payload, status, resolution, result,
                    created_at, expires_at, resolved_at)
                   VALUES (:id, :kind, :summary, :preview, :payload, :status, '', '{}',
                           :created_at, :expires_at, NULL)""",
                action,
            )
            self._conn.commit()
        return action["id"]

    def get_action(self, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_actions WHERE id = ?", (action_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_actions(
        self, status: str | None = None, kind_prefix: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM pending_actions"
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if kind_prefix:
            clauses.append("kind LIKE ?")
            params.append(f"{kind_prefix}%")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def resolve_action(
        self, action_id: str, status: str, resolution: str = "", result: dict | None = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE pending_actions
                   SET status = ?, resolution = ?, result = ?, resolved_at = ?
                   WHERE id = ?""",
                (status, resolution, json.dumps(result or {}), _now(), action_id),
            )
            self._conn.commit()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            def count(table: str) -> int:
                return int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

            return {
                "documents": count("documents"),
                "chunks": count("chunks"),
                "conversations": count("conversations"),
                "messages": count("messages"),
                "pending_proposals": int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM write_proposals WHERE status = 'pending'"
                    ).fetchone()[0]
                ),
                "pending_actions": int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM pending_actions WHERE status = 'pending'"
                    ).fetchone()[0]
                ),
                "embedder": self.embedder.name,
                "db_path": str(self.db_path),
            }
