# Billie

A local-first personal AI assistant. Single user, runs on your own machine,
keeps its memory in a local database, and tells you which model answered every
question.

**Status: Phase 1 complete.** Chat with routing between a local model and the
Claude API, memory over a notes folder with citations, a confidence flag on
every answer, audit logging, and a web UI. The remaining phases are listed in
[docs/ROADMAP.md](docs/ROADMAP.md).

---

## What works right now

| | |
|---|---|
| **Hybrid model backend** | Claude via the Anthropic API for reasoning; a local open-weight model via Ollama for offline, quick, and private requests |
| **Routing layer** | Decides per request from three inputs: your privacy flag, whether each model is reachable, and a complexity score. Always shown in the UI, with the reason |
| **Privacy flag** | A request marked private is answered on-device or not at all. It never falls back to the cloud, even if the local model dies mid-answer |
| **Memory (RAG)** | Your `notes/` folder plus facts you teach Billie directly, chunked, embedded, and retrieved before every answer |
| **Citations** | Retrieved sources are handed to the model as `[S1]`, `[S2]`… and cited back. The UI lists what was actually cited |
| **Confidence flag** | Every answer is labelled `grounded`, `unverified`, or `Billie flagged uncertainty` — an honest signal about whether the answer rested on a source |
| **Audit log** | Every request, model call, routing decision, and memory write goes to `data/audit.log.jsonl`, with credentials redacted |
| **Auth** | Bearer-token auth on the API, with a loopback exemption for local use. Required before phase 6 exposes it to your phones |
| **Tests** | 93 tests covering config, secrets hygiene, retrieval, routing, grounding, both provider wire protocols, and the HTTP API |

## Quick start

```bash
cp .env.example .env          # then paste your keys into .env
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements.txt
python run.py                 # http://127.0.0.1:8765
```

Full instructions, including Ollama and troubleshooting: **[docs/SETUP.md](docs/SETUP.md)**.

No API key yet? `python scripts/demo_server.py` runs the whole app with a
scripted stand-in model so you can see the interface working.

## Project layout

```
Billie/
├── .env                  your keys (git-ignored, never committed)
├── .env.example          the template, with every key documented
├── run.py                start the app
├── backend/billie/
│   ├── config.py         settings, all secrets from the environment
│   ├── app.py            FastAPI app factory and wiring
│   ├── assistant.py      retrieve -> route -> answer -> record
│   ├── grounding.py      system prompt, citations, confidence flag
│   ├── audit.py          append-only audit log with redaction
│   ├── auth.py           bearer-token auth, loopback exemption
│   ├── models/           anthropic_provider, ollama_provider, router
│   ├── memory/           embeddings, SQLite vector store, notes indexer
│   └── api/              chat, memory, and system routes
├── frontend/             the chat UI (served at /)
├── notes/                your notes -- Billie's memory (git-ignored)
├── data/                 database + audit log (git-ignored)
├── scripts/demo_server.py
├── tests/                93 tests
└── docs/                 SETUP, ARCHITECTURE, ROADMAP
```

## The rules this app is built on

1. **Secrets live in `.env`.** Nothing is hardcoded, `.env` is git-ignored, and
   a test fails the build if a credential-shaped string reaches a tracked file.
2. **Private means private.** Flagged requests go to the local model or
   nowhere. There is no code path from a private request to the cloud.
3. **No silent guessing.** Billie cites its source or says it doesn't have one,
   and the UI shows which of those happened.
4. **No telemetry.** Billie talks to the APIs you configure and nothing else.
5. **You can see what it did.** Every external call is in the audit log.

## Honest limits

- The confidence flag reports whether an answer *cited a retrieved source*. It
  cannot tell you whether a cited answer is *true*.
- The default offline embedder is lexical: it matches shared vocabulary, not
  paraphrase. Install `nomic-embed-text` in Ollama and set
  `BILLIE_USE_OLLAMA_EMBEDDINGS=true` for real semantic retrieval.
- Billie learns by storing and retrieving what you teach it, not by retraining
  a model. That is the practical option for one person's data.
- The local database is not yet encrypted at rest — that is scheduled work, see
  the roadmap.
