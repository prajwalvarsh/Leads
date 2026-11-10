"""HTTP wrapper for n8n (docs/n8n.md). Run: uvicorn pipeline.api:app --port 8000

No stage logic here: the same stage functions and runner as the CLI (pipeline.stages).
Every endpoint needs the header X-API-Key = PIPELINE_API_KEY from .env.

Runs are asynchronous: POST /runs takes the run lock, starts the stages in a background
thread and returns 202 {run_id}. When the run ends, the final status is POSTed to
callback_url (n8n passes its Wait node's resume URL).
"""
from __future__ import annotations

import hmac
import json
import threading
import time
from typing import Any

import duckdb
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from pipeline.config import env
from pipeline.contract import LOCK_PATH, Mode, RunLock, RunLocked, StageResult, make_ctx
from pipeline.db import connect, new_run_id
from pipeline.stages import StageFn, resolve_stages, run_stages

CALLBACK_ATTEMPTS = 5


def require_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = env("PIPELINE_API_KEY")
    if not expected:
        raise HTTPException(503, "PIPELINE_API_KEY is not set in .env (restart the API after adding it)")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(401, "missing or wrong X-API-Key header")


app = FastAPI(title="Heidi lead pipeline", dependencies=[Depends(require_key)])

# Runs started by this process (status while running); older or CLI runs are read from run_log.
_runs: dict[str, dict[str, Any]] = {}


class RunRequest(BaseModel):
    stages: list[str] = ["all"]
    mode: Mode = "incremental"
    params: dict[str, Any] = {}
    callback_url: str | None = None


def _db() -> duckdb.DuckDBPyConnection:
    try:
        return connect()
    except duckdb.IOException as e:   # another process (e.g. a CLI run) holds the database
        raise HTTPException(503, f"database busy: {e}") from None


def _rows(con: duckdb.DuckDBPyConnection, sql: str, args: list) -> list[dict[str, Any]]:
    cur = con.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _callback(url: str, payload: dict) -> str:
    """POST the final status; retry, because n8n may not be waiting yet for a very short run."""
    last = ""
    for attempt in range(CALLBACK_ATTEMPTS):
        try:
            r = httpx.post(url, json=payload, timeout=30)
            if r.status_code < 400:
                return f"ok {r.status_code}"
            last = f"HTTP {r.status_code}: {r.text[:200]}"
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(2 ** attempt)
    return f"failed after {CALLBACK_ATTEMPTS} attempts: {last}"


def _execute(run_id: str, fns: list[StageFn], params: dict[str, Any], callback_url: str | None, lock: RunLock) -> None:
    state = _runs[run_id]
    ctx = None
    try:
        ctx = make_ctx(params, run_id=run_id)
        status, _ = run_stages(ctx, fns, params, on_result=lambda r: state["stages"].append(r.model_dump(mode="json")))
    except Exception as e:   # e.g. the database could not be opened
        status = "failed"
        state["stages"].append(StageResult(run_id=run_id, stage="run", status="failed",
                                           errors=[f"{type(e).__name__}: {e}"]).finish().model_dump(mode="json"))
    state["status"] = status
    try:
        if callback_url:
            outcome = _callback(callback_url, {"run_id": run_id, "status": status, "stages": state["stages"]})
            state["callback"] = outcome
            if ctx:
                ctx.log("run", "callback", outcome)
    finally:
        if ctx:
            ctx.con.close()
        lock.__exit__()


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "run_active": LOCK_PATH.exists()}


@app.post("/runs", status_code=202)
def start_run(req: RunRequest) -> dict[str, str]:
    try:
        fns = resolve_stages(req.stages)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    run_id = new_run_id()
    lock = RunLock(run_id)
    try:
        lock.__enter__()   # released by the background thread when the run ends
    except RunLocked as e:
        raise HTTPException(409, str(e)) from None
    params = {**req.params, "mode": req.mode}
    _runs[run_id] = {"run_id": run_id, "status": "running", "stages_requested": req.stages, "stages": []}
    threading.Thread(target=_execute, args=(run_id, fns, params, req.callback_url, lock), daemon=True).start()
    return {"run_id": run_id}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    if run_id in _runs:
        return _runs[run_id]
    con = _db()
    try:
        rows = con.execute("SELECT key, value FROM run_log WHERE run_id = ? AND stage = 'run' ORDER BY ts",
                           [run_id]).fetchall()
    finally:
        con.close()
    if not rows:
        raise HTTPException(404, f"run {run_id} not found")
    end = [v for k, v in rows if k == "end"]
    return {"run_id": run_id, "status": end[-1] if end else "running (or interrupted)",
            "stages": [json.loads(v) for k, v in rows if k == "stage_result"]}


COMPANY_COLS = """c.company_id, c.company_name, c.status, c.tab, c.bezirk, c.street, c.postcode, c.website,
                  c.phone, p.priority, p.why AS priority_why, c.first_seen_run, c.last_changed_run"""


@app.get("/runs/{run_id}/changes")
def run_changes(run_id: str, include_excluded: bool = False) -> dict[str, Any]:
    """Companies added (first_seen_run) or changed (last_changed_run) in this run. By default only
    the companies tab; a later run that changes a company again moves its last_changed_run on."""
    con = _db()
    try:
        if not con.execute("SELECT 1 FROM run_log WHERE run_id = ? LIMIT 1", [run_id]).fetchone():
            raise HTTPException(404, f"run {run_id} not found")
        rows = _rows(con, f"""SELECT {COMPANY_COLS} FROM company c LEFT JOIN priority p USING (company_id)
                              WHERE (c.first_seen_run = ? OR c.last_changed_run = ?) AND (? OR c.tab = 'companies')
                              ORDER BY c.company_name""", [run_id, run_id, include_excluded])
    finally:
        con.close()
    new = [r for r in rows if r["first_seen_run"] == run_id]
    changed = [r for r in rows if r["first_seen_run"] != run_id]
    top = [r for r in new if r["priority"] == "High"]
    return {"run_id": run_id, "counts": {"new": len(new), "changed": len(changed), "new_high_priority": len(top)},
            "new": new, "changed": changed, "new_high_priority": top}


@app.get("/companies")
def companies(bezirk: str | None = None, status: str | None = None, tab: str = "companies") -> list[dict[str, Any]]:
    con = _db()
    try:
        return _rows(con, f"""SELECT {COMPANY_COLS} FROM company c LEFT JOIN priority p USING (company_id)
                              WHERE c.tab = ? AND (? IS NULL OR c.bezirk = ?) AND (? IS NULL OR c.status = ?)
                              ORDER BY c.company_name""", [tab, bezirk, bezirk, status, status])
    finally:
        con.close()
