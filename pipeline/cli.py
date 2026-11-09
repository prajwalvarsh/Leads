"""CLI wrapper: python -m pipeline.cli <stage> [--json] [--mode full|incremental] [--params JSON]

No stage logic here: build ctx, take the run lock, call run_<stage>, print the StageResult.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

import typer

from pipeline.budget import Budget
from pipeline.config import cfg
from pipeline.contract import Ctx, RunLock, RunLocked, StageResult, make_ctx
from pipeline.discover import SOURCES, run_discover, run_serper_pilot
from pipeline.crawl import run_crawl
from pipeline.extract import run_extract
from pipeline.resolve import run_resolve

app = typer.Typer(add_completion=False, no_args_is_help=True)
sys.stdout.reconfigure(line_buffering=True)  # progress lines show up in log files while a stage runs

JsonOpt = typer.Option(False, "--json", help="Print the StageResult as the last stdout line")
ModeOpt = typer.Option("full", "--mode", help="full | incremental")
ParamsOpt = typer.Option(None, "--params", help='JSON, e.g. \'{"config": {"sources": {"osm": {"enabled": false}}}}\'')


def _execute(stage_fn: Callable[[Ctx, dict], StageResult], as_json: bool, mode: str, params_json: str | None,
             extra: dict[str, Any] | None = None) -> None:
    params: dict[str, Any] = json.loads(params_json) if params_json else {}
    params.setdefault("mode", mode)
    params.update(extra or {})
    ctx = make_ctx(params)
    try:
        with RunLock(ctx.run_id):
            ctx.log("run", "start", json.dumps({"stage": stage_fn.__name__, "params": params}))
            result = stage_fn(ctx, params)
            ctx.log("run", "end", result.status)
    except RunLocked as e:
        result = StageResult(run_id=ctx.run_id, stage=stage_fn.__name__.removeprefix("run_"),
                             status="failed", errors=[str(e)]).finish()
    _print(result, as_json)
    raise typer.Exit(0 if result.status in ("ok", "skipped") else 1)


def _print(result: StageResult, as_json: bool) -> None:
    print(f"\n[{result.stage}] status={result.status}")
    for k, v in result.counts.items():
        print(f"  {k}: {v}")
    if result.api_calls:
        print(f"  api_calls: {result.api_calls}")
    for e in result.errors:
        print(f"  ERROR {e}")
    if as_json:
        sys.stdout.flush()
        print(result.model_dump_json())


@app.command()
def discover(
    as_json: bool = JsonOpt, mode: str = ModeOpt, params: str | None = ParamsOpt,
    source: list[str] = typer.Option(None, "--source", "-s", help=f"Subset of enabled sources: {', '.join(SOURCES)}"),
) -> None:
    """Stage 1: find candidates from all enabled sources."""
    _execute(run_discover, as_json, mode, params, {"sources": source} if source else None)


@app.command()
def resolve(as_json: bool = JsonOpt, mode: str = ModeOpt, params: str | None = ParamsOpt) -> None:
    """Stage 2: one website per candidate (source, same-company record, then Serper search)."""
    _execute(run_resolve, as_json, mode, params)


@app.command()
def crawl(as_json: bool = JsonOpt, mode: str = ModeOpt, params: str | None = ParamsOpt) -> None:
    """Stage 3: fetch home, Impressum and up to 2 more pages per website."""
    _execute(run_crawl, as_json, mode, params)


@app.command()
def extract(as_json: bool = JsonOpt, mode: str = ModeOpt, params: str | None = ParamsOpt) -> None:
    """Stage 4: Impressum -> legal data (LLM, regex cross-check, evidence check)."""
    _execute(run_extract, as_json, mode, params)


@app.command(name="serper-pilot")
def serper_pilot(as_json: bool = JsonOpt, params: str | None = ParamsOpt) -> None:
    """Dev: 5-point Serper Maps pilot; reports credits and the spacing that fits the cap."""
    _execute(run_serper_pilot, as_json, "full", params)


@app.command(name="serper-sample")
def serper_sample() -> None:
    """Dev: ONE Serper Maps request (Hausverwaltung, Berlin-Mitte), saved as test fixture."""
    from pipeline.sources import serper_maps

    ctx = make_ctx()
    with RunLock(ctx.run_id):
        c = cfg()["caps"]
        budget = Budget(ctx.con, "serper", c["serper_credits"], c["serper_total"], all_time=True)
        data = serper_maps.fetch_sample(budget, cfg()["serper_maps"]["zoom"])
        ctx.log("serper_sample", "credits", budget.used)
    places = data.get("places", [])
    print(f"saved {serper_maps.SAMPLE_PATH}")
    print("top-level keys:", list(data.keys()))
    print("places on page 1:", len(places))
    keys: dict[str, int] = {}
    for p in places:
        for k in p:
            keys[k] = keys.get(k, 0) + 1
    print("fields per place (count of places having it):", keys)
    print("credits field in response:", data.get("credits", "(none)"))


@app.command(name="all")
def run_all(as_json: bool = JsonOpt, mode: str = ModeOpt, params: str | None = ParamsOpt) -> None:
    """Full run (stages are added as they are built)."""
    _execute(run_discover, as_json, mode, params)


if __name__ == "__main__":
    app()
