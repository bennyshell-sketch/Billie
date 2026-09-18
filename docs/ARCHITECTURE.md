# Architecture

## The shape of one request

```
  UI / PWA / curl
        │  POST /api/chat/stream
        ▼
  auth.py ─── bearer token, loopback exemption
        ▼
  assistant.py
        │
        ├─ 1. retrieve   memory/store.py     hybrid vector + lexical search
        │                                     over notes and taught facts
        ├─ 2. route      models/router.py    privacy → availability → complexity
        ├─ 3. answer     models/anthropic_provider.py  (cloud)
        │                models/ollama_provider.py     (local)
        ├─ 4. score      grounding.py        did it cite a real source?
        └─ 5. record     memory/store.py     conversation history
                         audit.py            append-only JSONL
        ▼
  SSE: start → delta… → final
```

The `start` frame carries the routing decision and retrieved sources, so the UI
can show where an answer is coming from before the first token arrives. The
`final` frame carries the whole answer, the citations, the confidence flag, and
token usage.

## Routing

`models/router.py` decides in a fixed order of authority:

1. **Privacy.** `private: true` → the local model, or a clear error. There is
   no code path from a private request to the cloud: the mid-stream fallback
   checks `decision.private` and re-raises instead.
2. **Availability.** No API key or an unreachable API → local. No Ollama →
   cloud. Neither → an error naming both reasons. Failures are cached for
   `BILLIE_PROVIDER_COOLDOWN_SECONDS` so an offline provider fails fast instead
   of burning a timeout on every request.
3. **Complexity.** `score_complexity()` — a transparent heuristic over length,
   analytical vocabulary, code markers, conversation depth, and how much
   context was retrieved. At or above `BILLIE_COMPLEXITY_THRESHOLD` the request
   goes to Claude.

An explicit `route` overrides step 3, and turns step 2 into an error rather
than a silent switch — if you asked for Claude, you get Claude or an
explanation.

Routing deliberately does not call a model to decide which model to call.

## Memory

One SQLite file holds documents, chunks, vectors, conversations, and messages.

- **Documents** are notes (files in `notes/`), facts you teach directly, and —
  from a later phase — conversation summaries. Each is keyed by a stable
  `source_id` and a content hash, so re-indexing unchanged files costs nothing.
- **Chunks** are ~900 characters with overlap, split on paragraph boundaries.
- **Vectors** are stored with the name of the embedder that produced them.
  Search only ever compares vectors from the active embedder, so switching
  embedders can never silently mix incompatible vector spaces.
- **Search** is exact cosine similarity (a numpy dot product over every vector)
  blended 75/25 with lexical overlap. One person's notes fit in memory
  comfortably; an approximate index would add a dependency and lose recall for
  no benefit at this scale.

The default `HashingEmbedder` needs no model and no network, and is
deterministic — which is what makes retrieval testable. Stopwords are dropped
before hashing, because in a lexical embedder function words otherwise dominate
short questions. `OllamaEmbedder` is the semantic upgrade.

## Grounding and the confidence flag

`grounding.py` does three things: it writes the system prompt that requires a
citation or an admission of uncertainty; it renders retrieved chunks as `[S1]`,
`[S2]`… blocks; and it reads the finished answer back to classify it:

| Flag | Meaning |
|---|---|
| `grounded` | Cited at least one source that was actually provided |
| `self-reported-uncertain` | Cited nothing, but said it wasn't certain |
| `unverified` | Cited nothing and claimed no uncertainty — **or cited a tag that was never offered**, which is a fabricated citation |

This is an honest signal, not a truth guarantee. It tells you whether the
answer rested on something retrievable. It cannot tell you whether a cited
answer is correct.

## Security posture

Billie is not antivirus and does not claim to be. What it does:

- **Secrets** are read from the environment only. `.env` is git-ignored, and
  `tests/test_config_and_secrets.py` scans every tracked file for
  credential-shaped strings on each run.
- **The audit log** (`data/audit.log.jsonl`) records every model call, routing
  decision, memory write, and fallback, with keys and bearer tokens redacted by
  pattern before writing.
- **Auth** is a bearer token with a loopback exemption. With no token
  configured, non-loopback requests are refused outright rather than allowed.
- **Encryption at rest** for the database is scheduled, not done — see the
  roadmap. Today the SQLite file is protected only by your OS file permissions.
- **Per-tool permission grants** arrive with the tools they govern, starting in
  phase 2 (file access), so a grant always describes something real.

## Why these choices

**FastAPI + a static frontend, not Electron yet.** The backend is the product;
the UI is a page it serves. That same page becomes the phone PWA in phase 6
with no rewrite, and a Tauri window can wrap it later without changing
anything. Shipping a desktop shell in phase 1 would have been packaging work,
not assistant work.

**SQLite over Chroma.** Chroma is another service and another schema for one
person's notes. SQLite gives durability, transactions, one file to back up or
encrypt, and exact search. The store's interface is narrow (`upsert_document`,
`search`, `delete_document`), so swapping the backend later touches one file.

**Providers behind one interface.** `LLMProvider` has two methods: `stream` and
`health`. Both real providers and the test fakes implement it, which is why the
routing tests can prove the privacy guarantee without any network.

**Streaming everywhere.** Both providers stream; the API is SSE. A local 8B
model on a laptop takes a while to think, and watching it work beats watching a
spinner.
