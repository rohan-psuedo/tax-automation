# Tax Automaton

Turns invoices and other financial documents into checked accounting vouchers and posts
them to Tally. Self-hosted: it runs on the CA office's own PC or LAN server, and staff use
it in a browser.

See the product concept PDF in this folder for the full vision and MVP scope.

## Layout

```
backend/   FastAPI app (Python 3.12+), SQLAlchemy + Alembic, Tally connector
web/       Next.js review dashboard (proxies /api to the backend)
```

The AI and accounting layers only use the system-agnostic model in
`backend/app/schemas/canonical.py`. Only `backend/app/connectors/tally/` knows Tally XML,
so other accounting systems can be added as new connectors.

## Install in an office

See **INSTALL.md**: double-click `start.bat` (production mode; `-Lan` for other computers,
`-Rebuild` after an update). It updates the database safely (backup first) on every start.

## Measure reading accuracy

```bash
cd backend && uv run python -m app.extraction.evaluate --from-posted --limit 40
```

Re-reads posted (human-checked) entries with the AI service in use and reports per-field
accuracy against the 95% target, and whether the voucher would match what was posted. Shows
a cost estimate (at Claude's prices) and sends nothing until you add `--yes`. `--folder DIR`
uses invoices with `<name>.expected.json` answer files instead. Run it after changing the
service or model.

## AI services

Invoices are read by whichever AI service the office has a key for. The catalog is
`backend/app/extraction/services.py`; each service is served by one adapter:

| Service (id) | Adapter | Notes |
|---|---|---|
| Claude (`anthropic`) | Anthropic SDK, structured output | the reference adapter; PDF sent whole |
| Gemini (`gemini`) | google-genai | PDF sent whole; keys `AIza…` or `AQ.…` |
| OpenAI (`openai`) | OpenAI SDK | PDF sent whole |
| OpenRouter (`openrouter`), Groq (`groq`), Grok (`xai`), Mistral (`mistral`) | OpenAI-compatible chat completions at the service's address | PDFs go as page images plus their text layer |
| DeepSeek (`deepseek`) | OpenAI-compatible | text only: scans and photos can't be read |
| Other (`custom`) | OpenAI-compatible at an address the office enters | Together, Fireworks, Ollama, LM Studio; key optional |

An administrator pastes a key in **Settings › Tally and AI**. `PUT /api/settings` with
`ai_api_key` (no `ai_provider`) saves it under the service its format points to
(`services.detect()`: the longest matching start wins, so `sk-ant-` is Claude and `sk-or-`
OpenRouter, not OpenAI; `sk-` plus 32 hex digits is DeepSeek), or under the service in use
when the format says nothing. Saving a key, or sending `ai_provider` on its own, puts that
service in use; `ai_model` and `ai_base_url` go with `ai_provider` to edit a service without
switching to it. Each service keeps its own key (encrypted with the installation's secret,
see `app/security_box.py`) and model; keys are never returned, logged or audited, only a
hint such as `AIzaSyD…9Qx2`. `POST /api/settings/test-ai` checks the service in use without
reading a document, and `GET /api/settings/ai-models?service=<id>` lists the models a key can
use.

Documents skipped because AI reading wasn't set up (or because the key was rejected, or a
text-only service met a scan) are queued again when a change to the AI settings leaves
reading ready, unless someone has edited them since (`vouchers.requeue_waiting_for_ai`).

Environment variables (in `backend/.env`; a value saved in Settings wins):

| Variable | Meaning |
|---|---|
| `AI_PROVIDER` | service id to use; without it (and nothing chosen in Settings), the first service that is set up (a key; for `custom`, an address and a model), in this order: anthropic, gemini, openai, openrouter, groq, xai, deepseek, mistral, custom |
| `AI_MODEL` | model for the `AI_PROVIDER` service (Claude uses `CLAUDE_MODEL`) |
| `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `CLAUDE_EFFORT` | Claude |
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | Gemini |
| `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `XAI_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY` | the other services |
| `AI_BASE_URL`, `AI_API_KEY` | address and (optional) key of the Other (OpenAI-compatible) service |

See `backend/.env.example`.

## Run it for development

Needs [uv](https://docs.astral.sh/uv/) and Node.js 20+.

```bash
cd backend && uv sync && uv run alembic upgrade head
```

```bash
cd backend && uv run uvicorn app.main:app --port 8000
```

```bash
cd web && npm install && npm run dev
```

Open http://localhost:3000. The first visit asks you to create the administrator account.

Uploaded files are stored under `backend/data/storage/` and read by a background worker
that runs inside the API process. To run it as its own process instead, set
`RUN_WORKER_IN_PROCESS=false` for the API and start:

```bash
cd backend && uv run python -m app.pipeline.worker
```

### Tally

In TallyPrime, enable the HTTP/XML server: **Alt+Z (Exchange) › Configure ›
Client/Server Configuration** (older releases: F1 › Settings › Connectivity), set
*TallyPrime acts as* to **Both** (or Server), port **9000**, and keep a company open.
Set `TALLY_URL` in `backend/.env` if Tally runs on another machine.

No Tally handy? Run the mock, which speaks the same XML protocol:

```bash
cd backend && uv run python -m app.devtools.mock_tally --port 9000
```

TallyPrime's free Educational mode works for testing, but it only accepts voucher dates on
the 1st, 2nd and 31st of a month (so in February only the 1st and 2nd), and refuses other
days with a misleading "Voucher date is missing". Start the mock with `--educational` to
get the same behaviour.

The connector follows TallyPrime 3.0+ (current release 7.1), and still works with older
releases:

- New party ledgers carry their GST registration and address in the dated lists
  (`LEDGSTREGDETAILS.LIST`, `LEDMAILINGDETAILS.LIST`) applying from the first day of the
  books, plus the old flat fields. Syncing reads the entry in force today, falling back to
  the flat fields.
- Requests go as UTF-16 with `charset=utf-16`: with UTF-8, Tally returns names in Indian
  scripts as "?".
- Vouchers carry the party's GSTIN, state and place of supply, so Tally's GST returns
  don't list them as uncertain. Each voucher's `REMOTEID` is its own ID: sending it again
  updates the voucher in Tally instead of adding a second one.
- Before posting, the app checks that the company is open in Tally (Tally can otherwise
  import into the active company) and that no new ledger's name is already used there
  (Tally treats creating an existing name as changing that ledger).

## Tests

Always run pytest as its own process. The suite refuses to start if app code was imported
first (for example by a script that calls `pytest.main()`), because the tests would then use
the real database and storage.

```bash
cd backend && uv run pytest && uv run ruff check .
```

```bash
cd web && npx tsc --noEmit && npm run lint
```

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Foundations: API, DB, auth & roles, companies, dashboard shell | Done |
| 1 | Tally connector: companies, ledger/group sync, ledger creation, voucher posting | Done (verified against the mock; still to verify against real TallyPrime) |
| 2 | Document upload & parsing: PDF, images, Word, Excel, CSV; duplicates; viewer | Done |
| 3 | AI extraction with any supported service (Claude claude-opus-5-5 by default, Gemini, OpenAI, OpenRouter, Groq, xAI, DeepSeek, Mistral, any OpenAI-compatible); structured output, refusal fallback | Built and tested with fake clients; needs an API key to run for real |
| 4 | Accounting engine: purchase/sales/notes, party & ledger matching, GST & reverse-charge ledgers, round-off | Done |
| 5 | Validation: totals, GST split and rates, GSTIN checksum, duplicates per financial year, dates | Done |
| 6 | Review screen, manual entry, Post to Tally, Post all ready | Core done |
| 7 | Activity & per-entry history, office settings (Tally, AI key encrypted), team & roles, posting rules incl. auto-post, daily backups | Done |
| 8 | Pilot hardening: start script & install guide, safe updates, accuracy check tool, sign-in throttling, security headers, log files | Tooling done; the pilot itself needs an API key and real TallyPrime |
