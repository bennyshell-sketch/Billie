# Setup

Billie runs on Windows (the primary target), macOS, and Linux. Commands below
show Windows PowerShell first, with the macOS/Linux equivalent underneath.

## 1. Prerequisites

| | Needed for | Notes |
|---|---|---|
| **Python 3.11+** | everything | `python --version` |
| **An Anthropic API key** | cloud reasoning | Optional — Billie runs local-only without one |
| **Ollama** | the local model | Optional — Billie runs cloud-only without it |

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

167 tests, no network access, a temporary database and workspace per test. Run
them before each phase moves forward.

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

**Retrieval misses obvious matches.** The offline embedder is lexical. Switch
to `nomic-embed-text` (step 4), or lower `BILLIE_RETRIEVAL_MIN_SCORE`.

**Everything goes to Claude / everything stays local.** Tune
`BILLIE_COMPLEXITY_THRESHOLD`: lower sends more to Claude, higher keeps more
local. The reason line under each answer shows the score it computed.

**Port already in use.** Change `BILLIE_PORT` in `.env`.
