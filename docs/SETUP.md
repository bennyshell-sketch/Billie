# Setup

Billie runs on Windows (the primary target), macOS, and Linux. Commands below
show Windows PowerShell first, with the macOS/Linux equivalent underneath.

## 1. Prerequisites

| | Needed for | Notes |
|---|---|---|
| **Python 3.11+** | everything | `python --version` |
| **An Anthropic API key** | cloud reasoning | Optional — Billie runs local-only without one |
| **Ollama** | the local model | Optional — Billie runs cloud-only without it |
| **A Google OAuth client** | calendar | Optional — everything else works without it |
| **An ElevenLabs API key** | Billie speaking back | Optional — speech *in* works without it |

Billie needs at least one of the two model backends. It will tell you clearly
if neither is available rather than pretending to answer.

## 2. Get the code and install dependencies

```powershell
cd Billie
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Secrets

```powershell
Copy-Item .env.example .env
notepad .env
```

```bash
cp .env.example .env
```

Open `.env` in your editor and paste your keys in. **Paste them into the file,
not into a chat with any AI assistant.** `.env` is git-ignored, and a test in
the suite fails if a credential-shaped string ever lands in a tracked file.

For phase 1 the only key that does anything is:

```
ANTHROPIC_API_KEY=sk-ant-...        # from https://console.anthropic.com/settings/keys
```

The other keys in `.env.example` (ElevenLabs, Google, Amadeus, …) are
commented placeholders for later phases. Leave them blank for now.

### The API token

`BILLIE_API_TOKEN` secures the HTTP API. While you are only using Billie on
this machine you can leave it blank — requests from anywhere other than
`127.0.0.1` are refused outright in that case. Before phase 6 (phone access
over Tailscale) generate one:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

…paste it into `BILLIE_API_TOKEN`, and set
`BILLIE_ALLOW_LOOPBACK_UNAUTHENTICATED=false` if you want the local UI to
present it too.

## 4. The local model (optional but recommended)

Install Ollama from <https://ollama.com>, then:

```powershell
ollama pull llama3.1:8b        # ~4.7 GB, good general default
ollama serve                    # usually already running as a service
```

Alternatives that work well on a laptop: `qwen2.5:7b`, `llama3.2:3b` (smaller
and faster), `qwen2.5:14b` (better, needs ~10 GB RAM). Whatever you pull, set
it in `.env`:

```
BILLIE_OLLAMA_MODEL=llama3.1:8b
```

**Better retrieval (optional).** Billie's default embedder works offline with
no downloads, but it matches on shared words rather than meaning. For real
semantic retrieval:

```powershell
ollama pull nomic-embed-text
```

then set `BILLIE_USE_OLLAMA_EMBEDDINGS=true` in `.env`. Vectors are tagged
with the embedder that made them, so switching never mixes the two — but
existing notes are only re-embedded when their content changes, so delete
`data/billie.sqlite3` and re-index if you want everything converted at once.

Ollama runs open-weight models only. It cannot run Claude — Claude is reached
solely through the Anthropic API, using your key.

## 5. Choose what Billie can read

Drop `.md` or `.txt` files into `notes/` to get started. Billie indexes that
folder, plus `workspace/`, on every start.

To add your own folders, list them in `.env`:

```
BILLIE_INDEXED_FOLDERS=C:\Users\you\Documents\Research,C:\Projects\scripts
```

Comma-separated, and always explicit. **There is no "index my whole drive"
option.** A filesystem root, a drive letter, or your bare home folder is
refused with an explanation rather than walked, and the reason appears in the
index report and the log.

What gets indexed: Markdown, text, and code files (`.py`, `.js`, `.sql`,
`.json`, `.yaml`, and friends), plus `.pdf` and `.docx`. Skipped automatically:
`node_modules`, `.git`, `.venv`, `__pycache__`, `dist`, `build` and similar;
dotfiles; symlinks; anything over `BILLIE_MAX_INDEXED_FILE_MB` (8 MB by
default); and binaries. Anything skipped is listed in the index report with the
reason.

Re-index at any time from the **Re-index files** button, or:

```powershell
curl -X POST http://127.0.0.1:8765/api/memory/reindex
```

You usually won't need to: the watcher checks every
`BILLIE_WATCH_INTERVAL_SECONDS` (60 by default) and re-indexes only when a
file's modification time or size has changed. Set it to `0` to turn it off.

> **Scanned PDFs.** Extraction reads a PDF's text layer. A scan has none, so
> Billie reports "no extractable text" rather than indexing an empty file. OCR
> is not included.

## 5a. What Billie may do: permissions

Open **Files & permissions** in the UI. Two grants exist today, and each is
revoked on its own:

| Permission | Default | What it controls |
|---|---|---|
| `file.read` | **granted** | Indexing and searching your allow-listed folders. Naming a folder in `.env` is already the act of consent, so this starts on. Revoke it and indexing stops and file content drops out of answers — facts you taught Billie directly still work, because those live in its own database |
| `file.write` | **denied** | Applying a change to a workspace file. Billie cannot write anything until you grant this, and even then only through a diff you approved |

Later phases register their own grants (calendar, browsing, purchases) as those
capabilities arrive, so a switch in the UI always controls something real.

## 5b. How Billie writes a file

Billie writes to exactly one folder, `workspace/`, and never silently:

1. A change is **proposed** — this produces a unified diff and touches nothing.
2. The diff appears under **Files & permissions → Pending changes**, and the
   header shows a count of what is waiting.
3. You **approve** or **reject** it. Only an approval writes.

Three further guards:

- `file.write` must be granted, or the approval is refused with a 403.
- If the file changed after the diff was generated, the approval is refused
  (HTTP 409) rather than overwriting your edit. Ask for a fresh diff.
- The previous version of any overwritten or deleted file is copied to
  `data/workspace-backups/` first.

Unapproved proposals expire after `BILLIE_PROPOSAL_TTL_MINUTES` (2 hours).

Driving it from the command line:

```powershell
# Propose -- returns a diff, writes nothing
curl -X POST http://127.0.0.1:8765/api/workspace/propose `
  -H "Content-Type: application/json" `
  -d '{\"path\": \"drafts/plan.md\", \"content\": \"# Plan\\n\"}'

# Approve, using the id from that response
curl -X POST http://127.0.0.1:8765/api/workspace/proposals/<id>/approve
```

## 5c. Connect your Google Calendar (optional)

**In the Google Cloud console**, once:

1. Create a project (or reuse one) and enable the **Google Calendar API**.
2. Under *APIs & Services → Credentials*, create an **OAuth client ID** of type
   **Desktop app**.
3. Add `http://127.0.0.1:8765/api/calendar/oauth/callback` as an authorised
   redirect URI. (Use your own `BILLIE_PORT` if you changed it.)
4. Paste the client ID and secret into `.env`:

```
GOOGLE_CLIENT_ID=....apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...
```

While your OAuth client is in *Testing* mode, add your own Google address as a
test user, or Google will refuse the sign-in.

**In Billie**, open **Files & permissions → Calendar → Connect Google**. That
opens Google's consent screen in a browser tab; approve it and the tab comes
back to Billie saying it is connected.

Billie asks for two scopes, not full calendar access: `calendar.readonly` (read
events and see which calendars you have) and `calendar.events` (create, move
and delete events). It cannot create or delete whole calendars.

**Where the token lives.** Google returns a refresh token — a long-lived
credential — and it goes into your operating system's credential store:
Credential Locker on Windows, Keychain on macOS, Secret Service on Linux. Not
`.env`, not the database, never the audit log.

If no keychain backend exists (usually a headless Linux box), Billie **refuses
to store it** rather than quietly writing it to disk, and tells you what to
install. If you would rather accept a permission-protected file, set
`BILLIE_ALLOW_FILE_TOKEN_STORE=true` — Billie then says so in the Calendar
panel for as long as that is in force.

**What Billie does with it.** Events within
`BILLIE_CALENDAR_DAYS_PAST`/`DAYS_AHEAD` are pulled into memory every
`BILLIE_CALENDAR_SYNC_INTERVAL_SECONDS` (15 minutes by default), so a question
like *"when is the team standup and where?"* is answered with a citation to
`calendar/primary/<event id>` — the same retrieval and citation machinery your
notes use.

**Changing the calendar** works exactly like a workspace write: a change is
proposed, you see precisely what will happen — including a warning when
invitations will be emailed to other people — and nothing reaches Google until
you approve it. `calendar.write` must be granted as well.

```powershell
# Propose -- nothing is sent to Google
curl -X POST http://127.0.0.1:8765/api/calendar/events/propose `
  -H "Content-Type: application/json" `
  -d '{"summary": "Dentist", "start": "2027-04-01T09:00:00Z", "end": "2027-04-01T09:30:00Z"}'

# Approve, using the id from that response
curl -X POST http://127.0.0.1:8765/api/calendar/actions/<id>/approve
```

Disconnecting revokes the token with Google, forgets it locally, and drops the
synced events out of memory.

## 5d. Voice (optional)

Two halves, and they are very different in where they run.

### Speech to text — on this machine

Whisper runs locally through faster-whisper, which is in `requirements.txt`.
Nothing you say is uploaded to be turned into text: the browser records, the
audio is posted to Billie on localhost, decoded in memory, transcribed, and
discarded.

The first transcription downloads the model into `data/whisper-models` — about
140 MB for `base`. That one download needs internet; after it, transcription
works entirely offline. Sizes, smallest and fastest first:

| `BILLIE_WHISPER_MODEL` | Size | Notes |
|---|---|---|
| `tiny` | ~75 MB | Fast, noticeably less accurate |
| `base` | ~140 MB | The default; fine on a laptop CPU |
| `small` | ~460 MB | Better, still CPU-workable |
| `medium` / `large-v3` | 1.5–3 GB | Want a GPU (`BILLIE_WHISPER_DEVICE=cuda`, `BILLIE_WHISPER_COMPUTE_TYPE=float16`) |

**Push-to-talk:** the 🎙 button next to Send. Hold it and talk, release to
send; or tap it once to start and again to stop, if holding is awkward. What
Whisper heard appears in the hint line and is sent as your message.

> Browsers only give a page the microphone in a secure context. `localhost`
> counts, so this works over plain HTTP on this machine. Reaching Billie from
> your phone (phase 6) will need HTTPS.

### Speech out — through ElevenLabs

This half leaves your machine: the text of Billie's reply is sent to ElevenLabs
under your key and audio comes back. It is billed per character, so
`voice.speak` is **denied by default**. Turn it on under **Files &
permissions → Permissions**, then tick **Speak replies** in the composer.

```
ELEVENLABS_API_KEY=...
```

Pick a voice from the dropdown under **Voice** in the panel — your account's
preset voices (Rachel, Adam, and the rest) appear alongside any you have
cloned. `BILLIE_TTS_VOICE_ID` pins one if you would rather not choose each
time.

### Cloning your own voice

1. Put 5–10 minutes of clean recordings of yourself into `voice-samples/`.
   One speaker, no music, no background noise. `.mp3`, `.wav`, `.m4a`,
   `.flac`, `.ogg`, `.webm`.
2. Grant `voice.clone` under Permissions.
3. Open **Voice → Clone your voice**, tick the files, name the voice, and
   confirm that the recordings are of you.
4. Approve the upload. The preview lists the exact files and sizes first.

Billie uploads audio from `voice-samples/` and nowhere else — naming a path
outside it is refused. It cannot verify whose voice is in a file; what it can
do is keep the source folder narrow, make the step deliberate, and log it.
**Clone only your own voice.**

## 6. Run it

```powershell
python run.py
```

Open <http://127.0.0.1:8765>. The two chips in the header show which models are
reachable; hover for the reason if one is offline.

To see the UI without any keys or models installed:

```powershell
python scripts/demo_server.py      # http://127.0.0.1:8766, scripted replies
```

## 7. Run the tests

```powershell
python -m pytest
```

316 tests, no network access, a temporary database and workspace per test.
Google and ElevenLabs are stood up in memory (`tests/fake_google.py`,
`tests/fake_elevenlabs.py`), so their whole flows are exercised without an
account, and audio fixtures are encoded with PyAV so the decoding path runs on
real WebM/Opus. Run them before each phase moves forward.

## Using it

- **Route** — `Auto` lets the router decide; `Local only` and `Claude only`
  force a provider (and error clearly if that provider is down, rather than
  quietly using the other one).
- **Private** — pins the request to the local model. No cloud fallback under
  any circumstance.
- **Use memory** — turn off to ask a question without retrieving from notes.
- Every reply shows the model that produced it, why it was routed there, what
  it cited, and its confidence flag.

### Teaching Billie a fact

```powershell
curl -X POST http://127.0.0.1:8765/api/memory/teach `
  -H "Content-Type: application/json" `
  -d '{\"content\": \"My passport expires in March 2031.\", \"title\": \"Passport\"}'
```

Facts are retrieved and cited exactly like notes.

### Reviewing what Billie did

```powershell
Get-Content data\audit.log.jsonl -Tail 20
# or in the API: GET /api/audit?limit=50
```

Every model call, routing decision, memory write, and fallback is there.
Credentials are redacted before anything is written.

## Troubleshooting

**"No model available."** Neither backend is reachable. The message names both
reasons — usually no API key in `.env` and Ollama not running.

**"Claude API rejected the API key in .env."** The key is wrong, revoked, or
has a stray space. Re-copy it from the Anthropic console.

**"Ollama has no model named …"** Run the `ollama pull` command in the error.

**Answers are always flagged `unverified`.** Nothing is being retrieved. Check
that `notes/` has `.md`/`.txt` files in it, re-index, and try
`POST /api/memory/search` to see what retrieval returns for your query. If
`file.read` is revoked, nothing from disk is retrieved at all.

**"Permission 'file.write' is not granted."** Expected: that is the default.
Turn it on under **Files & permissions**.

**A folder in `BILLIE_INDEXED_FOLDERS` is being ignored.** Check
`GET /api/status` → `files.roots` for `exists: false`, and the startup log for
a refusal. Folders that are too broad (a drive root, your bare home folder) are
refused on purpose.

**An approval returns 409.** The file changed after the diff was generated.
Nothing was written; propose the change again against the current content.

**"Cannot store credentials securely."** No OS keychain was found. On Windows
and macOS this should not happen. On Linux, install a Secret Service provider
(`pip install secretstorage` with GNOME Keyring or KWallet running), or set
`BILLIE_ALLOW_FILE_TOKEN_STORE=true` to accept a file instead.

**"Google did not return a refresh token."** Google only issues one the first
time you approve. Remove Billie at
<https://myaccount.google.com/permissions> and connect again.

**Google sign-in says the app is blocked or unverified.** While your OAuth
client is in *Testing*, only accounts added as test users can sign in. Add your
own address under *OAuth consent screen → Test users*.

**The sign-in tab returns "did not match this session".** The `state` check
failed — usually an old tab. Start again from the Connect button.

**"Microphone unavailable" when you hold the talk button.** The browser refused
or found no input device. Check the site's microphone permission, and that you
are on `http://127.0.0.1` or `localhost` rather than a LAN address — browsers
only hand over the microphone in a secure context.

**The first transcription hangs, then errors.** It is downloading the Whisper
model. Give it a minute on a slow connection; if it fails, the message says so,
and a smaller `BILLIE_WHISPER_MODEL` downloads faster.

**"Speech to text needs `pip install faster-whisper`."** It is in
`requirements.txt`; you are probably running outside the virtualenv.

**Transcription is slow.** `base` on a laptop CPU runs roughly in real time.
Drop to `tiny`, or use a GPU with `BILLIE_WHISPER_DEVICE=cuda` and
`BILLIE_WHISPER_COMPUTE_TYPE=float16`.

**"ElevenLabs rate limit or character quota reached."** You are out of
characters for the month. Billie keeps working; only speaking stops.

**Retrieval misses obvious matches.** The offline embedder is lexical. Switch
to `nomic-embed-text` (step 4), or lower `BILLIE_RETRIEVAL_MIN_SCORE`.

**Everything goes to Claude / everything stays local.** Tune
`BILLIE_COMPLEXITY_THRESHOLD`: lower sends more to Claude, higher keeps more
local. The reason line under each answer shows the score it computed.

**Port already in use.** Change `BILLIE_PORT` in `.env`.
