# n8n orchestration — built last

Read `CLAUDE.md` first. Build this only after the tasks it orchestrates are done.

## Why HTTP, not the Execute Command node

n8n calls the pipeline over HTTP. That works the same on n8n Cloud and on a
self-hosted instance, keeps Python dependencies out of the n8n container, and
matches a Docker deployment.

## API (`pipeline/api.py`, FastAPI)

All endpoints require header `X-API-Key` = `PIPELINE_API_KEY` from `.env`.

| Method | Path | Does |
|---|---|---|
| GET | `/health` | Liveness check |
| POST | `/runs` | Body `{stages: ["all"] or [...], mode: "full"/"incremental", params: {...}, callback_url?}`. Starts a background run, returns `202 {run_id}`. Returns `409` if a run is active. |
| GET | `/runs/{run_id}` | Run status plus the list of `StageResult`s so far |
| GET | `/runs/{run_id}/changes` | Companies added or changed in that run (from `first_seen_run` / `last_changed_run`) |
| GET | `/companies` | Current companies as JSON, filterable by `bezirk`, `status` |

A full run takes far longer than an HTTP request should stay open, so runs
are always asynchronous. When the run ends, if `callback_url` was given, the
service POSTs the final run status (`run_id`, `status`, `StageResult`s) to it;
n8n passes its Wait node's resume URL here.

## Docker

- `Dockerfile` for the pipeline (Python + Playwright Chromium).
- `docker-compose.yml` with two services, `pipeline` and `n8n`, on one network;
  n8n reaches the API at `http://pipeline:8000`. DuckDB and cache on a volume.

## Workflow (`n8n/workflow.json`)

Seven nodes. Task 2's stages run inside the same run, and Task 3 is a document,
so neither adds nodes. The Google Sheet is written by the pipeline's `export`
stage, so there is no Google Sheets node.

1. **Schedule Trigger** — weekly, Monday 06:00 Europe/Berlin.
2. **HTTP Request** — `POST /runs` with `stages: ["all"]`, `mode: incremental`,
   `callback_url: {{ $execution.resumeUrl }}`.
3. **Wait** — resume "On Webhook Call", POST, with a time limit (e.g. 6 h).
   The pipeline POSTs the final run status to the resume URL.
4. **IF** — `status == "ok"`.
5. **Notify error** (false branch, Slack or email) — failing stage and its errors.
6. **HTTP Request** (true branch) — `GET /runs/{run_id}/changes`.
7. **Notify digest** (Slack or email) — counts per stage and the new or changed
   companies; once Task 2 exists, also new top-tier leads (same node, via expression).

Add a **Manual Trigger** next to the Schedule Trigger for demos.

## Definition of done

- `docker compose up` starts both services; `/health` answers.
- The workflow, triggered manually, completes a run and sends the notification.
- Workflow JSON exported to `n8n/workflow.json`; one screenshot saved for the approach doc.