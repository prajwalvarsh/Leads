"""Stage 1: discover candidates from all enabled sources.

Contract (CLAUDE.md): run_discover(ctx, params) -> StageResult.
- Sources are switched on/off by `sources.<name>.enabled` (config, overridable via params).
- Each source runs independently; a failing source is recorded in errors and the stage
  ends `failed`, but the other sources still run.
- A reached cost cap ends the source with the counts so far and the stage `failed`.
- Candidates are upserted by candidate_id, so reruns are safe. Incremental mode reruns all
  enabled sources too (spec); "new this run" counts candidates not in the DB before.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from pipeline.budget import Budget, CapExceeded
from pipeline.contract import Ctx, StageResult
from pipeline.http import Fetcher
from pipeline.models import Candidate
from pipeline.sources import associations, dasoertliche, elf880, gelbeseiten, osm, places, search, serper_maps

STAGE = "discover"
SOURCES = ["serper_maps", "places", "osm", "gelbeseiten", "dasoertliche", "11880", "search", "associations"]


def save(ctx: Ctx, cands: list[Candidate]) -> int:
    unique = {c.candidate_id: c for c in cands}
    if unique:
        ctx.con.executemany(
            "INSERT OR REPLACE INTO candidate VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [c.row() for c in unique.values()],
        )
    return len(unique)


def candidate_ids(ctx: Ctx) -> set[str]:
    return {r[0] for r in ctx.con.execute("SELECT candidate_id FROM candidate").fetchall()}


# ---------- directories ----------
SearchFn = Callable[[Fetcher, str, str], Awaitable[tuple[list[Candidate], dict]]]

# name: (search function, Bezirk -> locations, extra city-wide locations)
DIRECTORIES: dict[str, tuple[SearchFn, Callable[[str], list[str]], list[str]]] = {
    "gelbeseiten": (gelbeseiten.search, lambda b: [f"berlin-{b}"], []),
    "dasoertliche": (dasoertliche.search, dasoertliche.locations, ["Berlin"]),
    "11880": (elf880.search, lambda b: [f"berlin-{b}"], []),
}


def is_berlin_listing(c: Candidate) -> bool:
    """Directory rows with a postcode outside Berlin are spill-over from the directory's search."""
    return not c.postcode or "10115" <= c.postcode <= "14199"


async def _directory(f: Fetcher, name: str, terms: list[str], bezirke: list[str]) -> tuple[list[Candidate], list[dict]]:
    fn, locs, extra = DIRECTORIES[name]
    cands: list[Candidate] = []
    infos: list[dict] = []
    jobs = [(b, loc) for b in bezirke for loc in locs(b)] + [("(all)", loc) for loc in extra]
    for term in terms:
        for b, loc in jobs:
            found, info = await fn(f, term, loc)
            kept = [c for c in found if is_berlin_listing(c)]
            info.update(term=term, bezirk=b, location=loc, dropped_non_berlin=len(found) - len(kept))
            infos.append(info)
            cands += kept
            print(f"  {name:12} {term:22} {loc:28} {len(kept):4} kept {len(found) - len(kept):4} non-Berlin"
                  f"{'  ERROR ' + info['error'] if info.get('error') else ''}")
    return cands, infos


async def _directories(names: list[str], terms: list[str], bezirke: list[str]) -> tuple[dict, list]:
    async with Fetcher() as f:
        results = await asyncio.gather(*(_directory(f, n, terms, bezirke) for n in names))
        skips = [{"url": s.url, "reason": s.reason} for s in f.skips]
    return dict(zip(names, results)), skips


# ---------- stage ----------
def run_discover(ctx: Ctx, params: dict[str, Any] | None = None) -> StageResult:
    params = params or {}
    res = StageResult(run_id=ctx.run_id, stage=STAGE)
    only: list[str] | None = params.get("sources")  # optional subset for this call
    before = candidate_ids(ctx)
    per_source: dict[str, Any] = {}
    failed = False

    def wanted(name: str) -> bool:
        return ctx.source_enabled(name) and (only is None or name in only)

    def record(name: str, n: int, extra: dict | None = None) -> None:
        per_source[name] = n
        ctx.log(STAGE, f"{name}.candidates", n)
        for k, v in (extra or {}).items():
            res.counts[f"{name}.{k}"] = v
            ctx.log(STAGE, f"{name}.{k}", v)
        print(f"[discover] {name}: {n} candidates {extra or ''}")

    def error(name: str, msg: str) -> None:
        nonlocal failed
        failed = True
        res.errors.append(f"{name}: {msg}")
        ctx.log(STAGE, f"{name}.error", msg)
        print(f"[discover] {name}: ERROR {msg}")

    for name in SOURCES:
        if only is not None and name in only and not ctx.source_enabled(name):
            per_source[name] = "disabled"

    caps = ctx.cfg["caps"]

    # 1. Serper Maps
    if wanted("serper_maps"):
        sm = ctx.cfg["serper_maps"]
        budget = Budget(ctx.con, "serper", caps["serper_credits"], caps["serper_total"], all_time=True)
        try:
            client = serper_maps.SerperClient(budget)
            points = serper_maps.grid_points(sm["spacing_km"])
            st = serper_maps.sweep(ctx.con, client, points, sm)
            n = ctx.con.execute("SELECT count(*) FROM candidate WHERE source = 'serper_maps'").fetchone()[0]
            record("serper_maps", n, {
                "grid_points": len(points), "spacing_km": sm["spacing_km"], "calls": st.calls,
                "credits": st.credits, "places_returned": st.places_returned,
                "unique_places": st.unique_places, "kept_berlin": st.kept_berlin,
                "dropped_not_berlin": st.dropped_not_berlin, "new_per_query": st.new_per_query,
            })
        except CapExceeded as e:
            error("serper_maps", f"cap reached: {e}")
        except (RuntimeError, NotImplementedError) as e:
            error("serper_maps", str(e))
        res.api_calls["serper_credits"] = budget.used

    # (optional) Google Places, disabled while there is no billing account
    if wanted("places"):
        budget = Budget(ctx.con, "places_text_search", caps["places_text_search"])
        try:
            stats = places.discover(ctx.con, ctx.run_id, budget)
            record("places", stats["unique_places"], stats)
        except CapExceeded as e:
            error("places", f"cap reached: {e}")
        except RuntimeError as e:
            error("places", str(e))
        res.api_calls["places_text_search"] = budget.used

    # 2. OSM
    if wanted("osm"):
        try:
            record("osm", save(ctx, osm.discover()))
        except Exception as e:  # network / Overpass errors must not stop other sources
            error("osm", f"{type(e).__name__}: {e}")

    # 3. Directories
    dirs = [d for d in DIRECTORIES if wanted(d)]
    if dirs:
        results, skips = asyncio.run(
            _directories(dirs, ctx.cfg["directory_terms"], list(ctx.cfg["bezirke"].keys()))
        )
        for name in dirs:
            cands, infos = results[name]
            errs = [i for i in infos if i.get("error")]
            dropped = sum(i.get("dropped_non_berlin", 0) for i in infos)
            record(name, save(ctx, cands), {"searches": len(infos), "search_errors": len(errs),
                                           "dropped_non_berlin": dropped})
            for i in errs:  # blocked/disallowed sites are skipped and logged, not a stage failure
                ctx.log(STAGE, f"{name}.skip", f"{i['term']} / {i['location']}: {i['error']}")
        for s in skips:
            ctx.log(STAGE, "http.skip", f"{s['reason']} {s['url']}")
        res.counts["http_skips"] = len(skips)

    # 4. Search
    if wanted("search"):
        budget = Budget(ctx.con, "tavily", ctx.cfg["tavily_budgets"]["discover"], caps["tavily_credits"])
        try:
            cands, stats = search.discover(ctx.con, budget)
            record("search", save(ctx, cands), stats)
        except CapExceeded as e:
            error("search", f"cap reached: {e}")
        except RuntimeError as e:
            error("search", str(e))
        res.api_calls["tavily_credits"] = budget.used

    # 5. Associations
    if wanted("associations"):
        cands = associations.discover()
        if cands is None:
            per_source["associations"] = "no file"
            ctx.log(STAGE, "associations.skipped", "data/manual/associations.csv not found")
        else:
            record("associations", save(ctx, cands))

    after = candidate_ids(ctx)
    res.counts["candidates_per_source"] = per_source
    res.counts["candidates_in_db"] = dict(
        ctx.con.execute("SELECT source, count(*) FROM candidate GROUP BY 1 ORDER BY 1").fetchall()
    )
    res.counts["new_candidates"] = len(after - before)
    ctx.log(STAGE, "new_candidates", len(after - before))
    for k, v in res.api_calls.items():
        ctx.log(STAGE, f"api.{k}", v)
    return res.finish("failed" if failed else "ok")


# ---------- Serper Maps pilot (development time) ----------
def run_serper_pilot(ctx: Ctx, params: dict[str, Any] | None = None) -> StageResult:
    """5 points spread across Berlin, both queries, nothing stored. Picks the spacing for the
    full run so it stays under the credit cap; writes numbers + choice to run_log."""
    res = StageResult(run_id=ctx.run_id, stage="serper_pilot")
    sm = ctx.cfg["serper_maps"]
    caps = ctx.cfg["caps"]
    budget = Budget(ctx.con, "serper", caps["serper_credits"], caps["serper_total"], all_time=True)
    try:
        client = serper_maps.SerperClient(budget)
        points = serper_maps.pilot_points(sm["pilot_points"])
        st = serper_maps.sweep(ctx.con, client, points, sm, store=False)
    except (CapExceeded, RuntimeError, NotImplementedError) as e:
        res.errors.append(str(e))
        res.api_calls["serper_credits"] = budget.used
        return res.finish("failed")
    spacing, estimates = serper_maps.choose_spacing(st.credits_per_point or [], caps["serper_credits"])
    res.counts = asdict(st) | {
        "points": [asdict(p) for p in points],
        "new_per_call_avg": round(sum(st.new_per_call) / len(st.new_per_call), 2) if st.new_per_call else 0,
        "estimated_credits_by_spacing_km": estimates,
        "chosen_spacing_km": spacing,
    }
    res.api_calls["serper_credits"] = budget.used
    for k, v in res.counts.items():
        ctx.log("serper_pilot", k, v)
    if spacing is None:
        res.errors.append("no spacing fits under the credit cap")
        return res.finish("failed")
    return res.finish("ok")
