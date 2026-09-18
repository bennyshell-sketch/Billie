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

## Phase 2 — File indexing and search

- An allow-list of folders in `.env` (`BILLIE_INDEXED_FOLDERS`), each one
  explicitly added. Never the whole drive.
- More file types: PDF, DOCX, code files.
- A designated `workspace/` folder Billie may write to — **every write shown as
  a diff you approve before it is saved.** No silent edits.
- Per-tool permission grants (file read, file write) you can revoke
  individually, enforced in the API layer and recorded in the audit log.
- Watch mode so edits re-index without a manual trigger.

*Checks in before phase 3.*

## Phase 3 — Google Calendar

- OAuth device flow against your own Google account; the refresh token stored
  in the OS keychain, not in `.env`.
- Read, create, and move events. Anything that writes to the calendar shows you
  exactly what it will do and waits for a yes.

## Phase 4 — Voice

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
