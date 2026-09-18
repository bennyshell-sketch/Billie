# Architecture

## The shape of one request

```
  UI / PWA / curl
        │  POST /api/chat/stream
        ▼
  auth.py ─── bearer token, loopback exemption
        ▼
  permissions.py ─── is this tool granted? (fails closed)
        ▼
  assistant.py
        │
        ├─ 1. retrieve   memory/store.py     hybrid vector + lexical search
        │                                     over indexed files and taught facts
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

## Reading your files

`memory/indexer.py` walks the roots from `Settings.index_roots()` — your notes
folder, the workspace, and anything in `BILLIE_INDEXED_FOLDERS`. There is no
mode that walks everything: a root that is a filesystem root, a drive letter,
or your bare home folder is refused by `refusal_reason()` with a message, and
that refusal is reported rather than logged and forgotten.

Each file is keyed `<root-name>/<relative path>`, which is also how it is cited,
so an answer always says which folder it came from. Extraction
(`memory/extract.py`) handles text and code directly, PDFs via pypdf, and DOCX
via python-docx; both libraries are optional, and a missing one produces an
"install X" line in the index report instead of a crash. Anything skipped —
symlinks, oversized files, scans with no text layer, binaries — is reported
with its reason.

`watcher.py` polls those roots on an interval, comparing a snapshot of paths,
modification times, and sizes. It reads no file contents, so an idle pass costs
a directory walk; re-indexing only happens when that snapshot changes. Polling
rather than OS file-system events keeps it dependency-free and identical across
Windows, macOS, and Linux.

## Permissions

`permissions.py` holds a registry of named tools, each with a description and a
default. State lives in the database, so a grant survives a restart; the audit
log records every change and every denial.

Defaults are per-tool rather than uniform. `file.read` starts granted because
naming a folder in `BILLIE_INDEXED_FOLDERS` is already the explicit act of
consent — the allow-list *is* the grant. `file.write` starts denied, because
nothing about configuring Billie implies permission to change your files.

Revoked means *fails closed*, never *degrades quietly*: `reindex_files()` and
the approve endpoint raise `PermissionDenied` (HTTP 403, naming the tool), and
retrieval with `file.read` revoked returns no file-sourced chunks at all rather
than a thinner set.

## Writing files

`workspace.py` has one rule: proposing and writing are different operations,
and only the second one touches disk.

A proposal computes a unified diff, records the hash of the file as it was at
that moment, and stores the whole thing as `pending`. Approval re-reads the
file, compares hashes, and refuses with `ProposalConflict` (HTTP 409) if
anything changed — so a diff you approved an hour ago cannot silently discard
an edit you made in between. The write itself goes to a temporary file that is
then renamed over the target, so an interrupted write cannot leave a truncated
file. The previous version is copied to `data/workspace-backups/` first.

Every path is confined to the workspace after symlink resolution, so `../`,
an absolute path, and a symlink pointing outside are all refused by the same
check rather than by three separate special cases.

## Anything external-facing

`workspace.py` split proposing from writing for files. `confirmations.py` is
the same shape for everything else: an action is proposed with a one-line
summary and a human-readable preview, and an *executor* -- supplied by whoever
owns the action -- runs only when you approve. The queue itself never learns
what a calendar event or a flight booking is, which is why phase 7's travel
booking will use it unchanged.

The executor is the only path to the outside world. Approving twice is refused,
a failing executor marks the action `failed` and keeps the error, and an
unapproved action expires rather than sitting there indefinitely.

## Google Calendar

Three pieces, deliberately separate:

- `integrations/token_store.py` decides *where a credential lives*. The OS
  keychain, or nowhere: with no keychain available it raises rather than
  silently writing a refresh token to disk, and the file fallback only exists
  behind `BILLIE_ALLOW_FILE_TOKEN_STORE`. The UI shows which is in use.
- `integrations/google_oauth.py` runs the authorisation-code flow with PKCE and
  a loopback redirect. Google's device flow would be a smaller change to the UI
  but does not cover Calendar scopes, and the loopback flow is what Google
  recommends for an installed app. Billie already runs a local server, so its
  own port is the redirect target. Access tokens are refreshed a couple of
  minutes before expiry rather than after a failed call.
- `integrations/google_calendar.py` talks to the Calendar REST API with httpx.
  Reads return immediately; every write is a proposal on the confirmation
  queue, and the preview spells out what will change -- including a line naming
  anyone who will be emailed an invitation, because that is the part of
  "create an event" that reaches other people.

`integrations/calendar_sync.py` pulls events into memory as documents keyed
`calendar/<calendar-id>/<event-id>`. That means "what have I got on Thursday?"
is answered by the retrieval and citation machinery that already exists,
instead of teaching the model a new tool. A sync that fails against Google
does not sweep memory, so a transient outage cannot empty your diary.

## Memory

One SQLite file holds documents, chunks, vectors, conversations, messages,
permission grants, write proposals, and pending actions.

- **Documents** are files (from any indexed root), calendar events, facts you
  teach directly, and — from a later phase — conversation summaries. Each is keyed by a stable
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
- **Per-tool permission grants** arrive with the tools they govern, so a grant
  always describes something real. Today: `file.read`, `file.write`,
  `calendar.read`, `calendar.write`.
- **File writes** are confined to one folder, gated on a grant, and require an
  approved diff. There is no API that writes a file directly.
- **OAuth tokens** live in the OS keychain, never in `.env` or the database.
  The audit log records that a token was refreshed, never the token.
- **Calendar changes** are gated on a grant *and* a per-action confirmation.
  There is no API that changes your calendar directly.

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

**Proposals in the database, not in memory.** A pending diff survives a
restart, which matters once you can propose from a phone and approve at a
desk (phase 6). It also means the audit trail and the proposal record agree
about what was pending and when.

**Polling for file changes, not OS events.** `watchdog` would be lower-latency
and one more dependency with three platform backends. A snapshot of mtimes at a
60-second interval is enough for notes you are editing yourself, and behaves
identically everywhere.

**httpx against Google's REST API, not the Google client libraries.**
`google-api-python-client` plus `google-auth-oauthlib` is a large dependency
tree to reach five endpoints, and it hides the token handling that most needs
to be visible in an app whose whole claim is that you can see what it did. The
OAuth exchange and refresh are about thirty lines here, and
`tests/fake_google.py` can stand the whole thing up in memory.

**Calendar events as retrievable documents, not a model tool.** Wiring tool-use
into the model loop is a bigger change than phase 3 needed, and it would make
the model responsible for deciding when to touch your calendar. Syncing events
into memory gets the useful half — asking about your schedule and getting a
cited answer — while changes stay something you ask for and approve.
