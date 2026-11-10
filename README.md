# Berlin property managers: lead list for Heidi Systems

A pipeline that finds property management companies (Hausverwaltungen) in Berlin and
qualifies them as leads for Heidi Systems' metering service. It collects candidates from
Das Örtliche, Gelbe Seiten, 11880, OpenStreetMap and Google Maps (via Serper), then
crawls each company's own website. It reads company data from the Impressum, checks
whether the company is a property manager and is in Berlin, and merges duplicates. The
latest run found **1,549 companies**. Of these, **804 are confirmed**: 442 from their
own website and 362 from a property-management directory listing (no website). The
other **745 are kept for review**. For the 442 confirmed companies with a website,
the pipeline reads qualifying KPIs (segments, units managed, metering provider, remote
reading, digital tools, decision maker) and assigns a priority: **38 High, 192 Medium,
212 Low**. It found **418 decision makers**, 42 emails (35 published, 7 by pattern)
and 63 LinkedIn profiles. Every value in the sheet keeps its source URL and a verbatim
quote. A blind spot check against 20 random Impressum pages, read by a separate model,
matched 18–19 of 19 rows on every field (`docs/spotcheck.md`).



## How it works

Every stage is one function, `run_<stage>(ctx, params) -> StageResult`. The CLI and the
HTTP API both call it. All state lives in one DuckDB file. Stages are idempotent, so any
stage can be rerun on its own. Thresholds, sources, models and call caps are set in
`config/berlin.yaml`.

| # | Stage | File | What it does |
|---|---|---|---|
| 1 | discover | `pipeline/discover.py`, `pipeline/sources/*.py` | Collects candidates from the directories, OpenStreetMap and Google Maps via Serper. Only the Google CID is stored; other Google fields live in a temp table that is dropped at export. |
| 2 | resolve | `pipeline/resolve.py` | Finds each candidate's own website and rejects shared hosts and portals. |
| 3 | crawl | `pipeline/crawl.py`, `pipeline/http.py` | Fetches the Impressum and up to 2 more pages. Checks robots.txt, waits 1 s between requests to the same host, and caches pages on disk. Uses Playwright only when the plain HTML has no usable text. |
| 4 | extract | `pipeline/extract.py` | Reads name, address, phone, register number and legal form with regex first, then an LLM for missing fields. A value is kept only if its quote appears verbatim on the page. |
| 5 | classify | `pipeline/classify.py` | Decides whether the company is a property manager: keyword rules plus an LLM yes/no that must give a quote. |
| 5b | investigate | `pipeline/investigate.py` | Optional agent for the review queue (switched off, returns `skipped`). |
| 6 | dedupe | `pipeline/dedupe.py` | Merges candidates into companies by register number, domain, phone, or fuzzy name plus street. |
| 7 | geo | `pipeline/geo.py`, `pipeline/berlin_geo.py` | Checks the Berlin postcode and city, geocodes with Nominatim (cached) and assigns the Bezirk by point-in-polygon. |
| 8 | confirm | `pipeline/confirm.py` | Sets the final status: confirmed (website), confirmed (listing) or review. Applies the manual overrides from config. |
| 9 | crawl_more | `pipeline/crawl_more.py` | Fetches up to 3 more pages per confirmed company (team, jobs, references) for KPI extraction. |
| 10 | kpi_extract | `pipeline/kpi.py` | One LLM call per confirmed company. Every KPI comes with a verbatim quote. |
| 11 | priority | `pipeline/priority.py` | Assigns High, Medium or Low from the KPIs (rules in `docs/task2.md`). |
| 12 | linkedin | `pipeline/linkedin.py` | Searches LinkedIn profile URLs for decision makers via Serper. LinkedIn pages are never opened. |
| 13 | export | `pipeline/export.py` | Writes the Google Sheet tabs and drops all temporary Google data. |
| – | spotcheck | `pipeline/spotcheck.py` | Independent check: refetches 20 random Impressum pages and has a separate model read them blind. |

Shared code: `pipeline/stages.py` (stage order and runner), `pipeline/cli.py` (CLI),
`pipeline/api.py` (HTTP API for n8n), `pipeline/db.py` (schema), `pipeline/llm.py`
(OpenRouter client), `pipeline/budget.py` (call caps), `pipeline/normalize.py`
(phone, domain, name and register number normalization). Prompts are in
`prompts/<role>_v<N>.md`.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env              # then fill in the keys
```

`.env` keys (never commit `.env` or `sa.json`):

| Key | Used for |
|---|---|
| `SERPER_API_KEY` | Google Maps and web search via Serper (required) |
| `OPENROUTER_API_KEY` | LLM calls (models set per role in `config/berlin.yaml`) |
| `TAVILY_API_KEY` | Fallback search and page extraction |
| `GOOGLE_SA_JSON` | Path to the Google service-account JSON (default `./sa.json`) |
| `SHEET_ID` | Target Google Sheet. Share the sheet with the service account's email as Editor. |
| `PIPELINE_API_KEY` | Any long random string. Every API request must send it as the `X-API-Key` header. |
| `GOOGLE_PLACES_API_KEY` | Optional; the Places source is disabled |

## Run

### CLI

```bash
python -m pipeline.cli all --mode full --json      # full run, every stage in order
python -m pipeline.cli all --mode incremental      # only new or changed records
python -m pipeline.cli <stage> --json              # one stage, e.g. geo, confirm, export
python -m pipeline.cli spotcheck --params '{"seed": 20261110}'
pytest -q
```

Stage names with an underscore use a hyphen on the CLI (`crawl-more`, `kpi-extract`).
With `--json`, the last stdout line is the `StageResult`. The exit code is 0 on ok or
skipped and 1 on failure. A lock file stops a second run from starting while one is
active.

### API + n8n

```bash
uvicorn pipeline.api:app --port 8000
```

| Method | Path | Does |
|---|---|---|
| GET | `/health` | Liveness check |
| POST | `/runs` | Starts a background run (`stages`, `mode`, `params`, `callback_url`) and returns `202 {run_id}`; `409` if a run is active |
| GET | `/runs/{run_id}` | Run status and stage results |
| GET | `/runs/{run_id}/changes` | Companies added or changed in that run |
| GET | `/companies` | Current companies, filterable by `bezirk`, `status` |

To set up n8n:

1. In n8n, go to **Workflows → Import from file** and import `n8n/workflow.json`.
2. Create a **Header Auth** credential with name `X-API-Key` and your `PIPELINE_API_KEY`
   as the value. Select it in the *Start run* and *Get changes* nodes.
3. The workflow calls the API at `http://127.0.0.1:8000`. Change the URLs in those two
   nodes if the API runs somewhere else.

The workflow runs every Monday at 06:00 (there is also a manual trigger for demos). It
starts an incremental run and waits for the pipeline to call back. Then it fetches the
changes and builds a digest, or an error message if a stage failed.

## Where outputs go

| Output | Location |
|---|---|
| Lead list | The Google Sheet. Tabs: Leads, Other finds, Details, KPI definitions, Priority rules, Evidence, Sources, Excluded, Not in Berlin, Run log. |
| Database | `data/leads.duckdb`: all candidates, pages, facts with quotes, companies, KPIs, run log (gitignored) |
| HTTP and geocoding cache | `data/cache/` (gitignored) |
| Database backups | `data/backups/` (gitignored) |
| Berlin boundaries | `data/geo/` (Berlin open data, CC BY; committed) |
| Spot check | `docs/spotcheck.md` (report), `eval/spotcheck_results*.json` (raw results) |
| Run history | `run_log` table in the database and the *Run log* tab |
| n8n workflow | `n8n/workflow.json` |

Data sources: Das Örtliche, Gelbe Seiten, 11880, Google Maps (via Serper; only the CID is
stored) and OpenStreetMap (© OpenStreetMap contributors).
