# Roadmap

The build order from the brief. Each phase ends with tests and a check-in
before the next one starts.

---

## ✅ Phase 1 — Chat, routing, memory, secrets *(complete)*

Local chat app talking to the Claude API and a local model, with a routing
layer, RAG over a notes folder, citations, a confidence flag, audit logging,
and `.env`-based secrets.

**Delivered:** `backend/billie/` (config, assistant, router, two providers,
memory store, grounding, audit, auth, API), `frontend/` chat UI, 93 tests,
setup guide.

---

## ✅ Phase 2 — File indexing and search *(complete)*

- An allow-list of folders in `.env` (`BILLIE_INDEXED_FOLDERS`), each one
  explicitly added. Roots that are too broad — a filesystem root, a drive
  letter, your bare home folder — are refused with a reason.
- Markdown, text, code, PDF (pypdf) and DOCX (python-docx). Noise directories,
  dotfiles, symlinks, and oversized files are skipped and reported.
- A `workspace/` folder Billie may write to, and only by proposing a unified
  diff you approve. Stale diffs are refused rather than clobbering an edit;
  the previous version is backed up; writes are atomic.
- Per-tool permission grants (`file.read`, `file.write`), revocable
  individually in the UI, enforced in the service layer, audited on every
  change and every denial.
- A background watcher that re-indexes changed files without a manual trigger.

**Delivered:** `memory/extract.py`, `memory/indexer.py`, `permissions.py`,
`workspace.py`, `watcher.py`, `api/files.py`, the Files & permissions panel in
the UI, and 74 further tests (167 total).

## ✅ Phase 3 — Google Calendar *(complete)*

- OAuth against your own Google account, with the refresh token in the OS
  keychain rather than `.env`. **Changed from the plan:** the authorisation-code
  flow with PKCE and a loopback redirect, not the device flow — Google's device
  flow does not cover Calendar scopes, and the loopback flow is what they
  recommend for an installed app. Billie's own port is the redirect target.
- With no keychain available, Billie refuses to store the token rather than
  quietly writing it to disk; the file fallback is opt-in and flagged in the UI.
- Read, create, move, and delete events. Every change is proposed with a preview
  of exactly what will happen — including who will be emailed an invitation —
  and waits for an explicit approval, gated on the `calendar.write` grant.
- Events sync into memory as citable documents, so questions about your schedule
  are answered by the existing retrieval and citation machinery.
- A general confirmation queue (`confirmations.py`) that phase 7's travel
  booking reuses.

**Delivered:** `confirmations.py`, `integrations/` (token store, OAuth,
calendar, sync), `api/calendar.py`, the Calendar section of the UI panel, and
86 further tests (253 total), including an in-memory Google
(`tests/fake_google.py`).

**Not done, deliberately:** the model cannot decide on its own to create or
move an event. Tool-use in the model loop is a later step; for now a calendar
change is something you ask for and approve.

## Phase 4 — Voice *(next)*

- **Speech to text:** `faster-whisper` running locally. Audio is transcribed
  on-device; no recording is uploaded to convert it to text.
- **Text to speech:** ElevenLabs (`textToSpeech.convert`), with Instant Voice
  Cloning from a 5–10 minute sample of your own voice, plus two or three of
  their preset voices to switch between. Only your own recordings, only under
  your own ElevenLabs account.
- Push-to-talk in the web UI; wake word (Porcupine) as an opt-in extra.

## Phase 5 — Literature and research

- PubMed/NCBI E-utilities and Semantic Scholar, cited with DOIs and links —
  these flow into the same citation and confidence machinery that already
  exists, so a paper is a source like any other.
- Career development plans you can mark progress on, which feed into later
  suggestions.
- Coding-tutor mode: lessons, review of your own scripts, exercises at a level
  you set.

## Phase 6 — Phone access

- Tailscale for the network: Billie binds to its Tailscale interface, reachable
  from your iPhone and Samsung, never exposed on the open internet and never
  through a third-party relay.
- `BILLIE_API_TOKEN` becomes mandatory; the loopback exemption is off by
  default from here on.
- The existing UI becomes an installable PWA (manifest, service worker,
  offline shell) — the same interface, no rewrite.

## Phase 7 — Travel search

- Amadeus for Developers for flight and hotel search.
- **Booking and payment require an explicit confirmation every time**, showing
  the exact itinerary, price, and what will be charged. Never autonomous.
- Their free tier is sandbox data. Real bookings need their paid production
  tier — the sandbox will be clearly labelled as such in the UI.

## Phase 8 — Image and video, and hardening

- Image generation behind the same confirmation and audit model.
- Video generation via a third-party API (e.g. Runway) — a separate paid
  service, not something Claude produces.
- **Encryption at rest:** SQLCipher for the database, OS keychain for
  credentials.
- Optional plain-language summaries of Windows Defender's event log —
  summarising an existing security tool, not replacing one.
- Desktop shell: a Tauri window plus a system-tray background service.

---

## Standing rules for every phase

- Anything irreversible or external-facing — sending an email, booking travel,
  spending money, deleting a file — requires an explicit "yes, do it" that
  shows exactly what will happen. Agentic multi-step work is fine for research,
  drafting, and organising.
- Every external call, file write, and tool use is audited.
- Every reply keeps showing which model produced it and whether it was
  grounded.
- No telemetry. Billie talks only to the APIs you configured, each under your
  own account.
