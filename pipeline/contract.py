"""n8n-ready stage contract (CLAUDE.md): run_<stage>(ctx, params) -> StageResult.

The CLI (and later the HTTP API) only build a Ctx, call the stage function and print the
StageResult. No stage logic lives in the wrappers.
"""
from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import duckdb
from pydantic import BaseModel, Field

from pipeline.config import DATA, cfg
from pipeline.db import connect, log, new_run_id, now

Status = Literal["ok", "failed", "skipped"]
Mode = Literal["full", "incremental"]
LOCK_PATH = DATA / "pipeline.lock"


class StageResult(BaseModel):
    run_id: str
    stage: str
    status: Status = "ok"
    started_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
    counts: dict[str, Any] = Field(default_factory=dict)
    api_calls: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)

    def finish(self, status: Status | None = None) -> "StageResult":
        if status:
            self.status = status
        self.finished_at = now()
        return self


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Ctx:
    con: duckdb.DuckDBPyConnection
    run_id: str
    mode: Mode = "full"
    cfg: dict[str, Any] = field(default_factory=cfg)

    def log(self, stage: str, key: str, value: object) -> None:
        log(self.con, self.run_id, stage, key, value)

    def source_enabled(self, name: str) -> bool:
        return bool(self.cfg.get("sources", {}).get(name, {}).get("enabled", False))


def make_ctx(params: dict[str, Any] | None = None, run_id: str | None = None) -> Ctx:
    """params: {"mode": "full"|"incremental", "config": {...overrides of berlin.yaml...}}"""
    params = params or {}
    merged = deep_merge(cfg(), params.get("config", {}))
    return Ctx(con=connect(), run_id=run_id or new_run_id(), mode=params.get("mode", "full"), cfg=merged)


class RunLocked(RuntimeError):
    pass


class RunLock:
    """One run at a time (DuckDB has a single writer). Fails fast if the lock exists."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id

    def __enter__(self) -> "RunLock":
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            other = LOCK_PATH.read_text(encoding="utf-8", errors="replace")
            raise RunLocked(
                f"another run is active (lock {LOCK_PATH}: {other}). "
                "If no run is active, delete the lock file."
            ) from None
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"run_id": self.run_id, "pid": os.getpid(), "started": now().isoformat()}))
        return self

    def __exit__(self, *exc: object) -> None:
        LOCK_PATH.unlink(missing_ok=True)
