"""Stage registry and runner, shared by the CLI and the HTTP API (no stage logic here).

A run executes stages in order under one run_id and stops at the first failed stage.
Every StageResult is written to run_log (stage='run', key='stage_result'), so a run's
results can be fetched later by run_id from any process.
"""
from __future__ import annotations

import json
import traceback
from collections.abc import Callable
from typing import Any

from pipeline.classify import run_classify
from pipeline.confirm import run_confirm
from pipeline.contract import Ctx, StageResult
from pipeline.crawl import run_crawl
from pipeline.crawl_more import run_crawl_more
from pipeline.dedupe import run_dedupe
from pipeline.discover import run_discover
from pipeline.export import run_export
from pipeline.extract import run_extract
from pipeline.geo import run_geo
from pipeline.investigate import run_investigate
from pipeline.kpi import run_kpi_extract
from pipeline.linkedin import run_linkedin
from pipeline.priority import run_priority
from pipeline.resolve import run_resolve

StageFn = Callable[[Ctx, dict], StageResult]

# Order of a full run ("all"): Task 1, then Task 2, then export (writes every tab).
STAGES: dict[str, StageFn] = {
    "discover": run_discover,
    "resolve": run_resolve,
    "crawl": run_crawl,
    "extract": run_extract,
    "classify": run_classify,
    "investigate": run_investigate,
    "dedupe": run_dedupe,
    "geo": run_geo,
    "confirm": run_confirm,
    "crawl_more": run_crawl_more,
    "kpi_extract": run_kpi_extract,
    "priority": run_priority,
    "linkedin": run_linkedin,
    "export": run_export,
}


def resolve_stages(names: list[str]) -> list[StageFn]:
    """["all"] -> every stage in order; otherwise the named stages, in the order given."""
    if names == ["all"]:
        return list(STAGES.values())
    unknown = [n for n in names if n not in STAGES]
    if unknown or not names:
        raise ValueError(f"unknown stages {unknown}; use ['all'] or a subset of {list(STAGES)}")
    return [STAGES[n] for n in names]


def run_stages(ctx: Ctx, fns: list[StageFn], params: dict[str, Any],
               on_result: Callable[[StageResult], None] | None = None) -> tuple[str, list[StageResult]]:
    """Run the stages in order; stop at the first failure. Returns (run status, results)."""
    ctx.log("run", "start", json.dumps({"stages": [f.__name__.removeprefix("run_") for f in fns], "params": params}))
    results: list[StageResult] = []
    status = "ok"
    for fn in fns:
        try:
            result = fn(ctx, params)
        except Exception as e:   # a crashing stage is a failed stage, not a dead run
            traceback.print_exc()
            result = StageResult(run_id=ctx.run_id, stage=fn.__name__.removeprefix("run_"), status="failed",
                                 errors=[f"{type(e).__name__}: {e}"]).finish()
        ctx.log("run", "stage_result", result.model_dump_json())
        results.append(result)
        if on_result:
            on_result(result)
        if result.status == "failed":
            status = "failed"
            break
    ctx.log("run", "end", status)
    return status, results
