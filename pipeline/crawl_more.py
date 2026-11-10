"""Task 2, stage crawl_more: up to 3 more pages per confirmed company (team, jobs, references).

Same crawler as Task 1 (robots.txt, 1 req/s per host, disk cache, Playwright fallback). Links come
from the home page, which is in the HTTP cache, so finding them needs no new request. A URL that is
already in `page` is not fetched again. No Tavily calls (no new paid sources).
"""
from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from pipeline.contract import Ctx, StageResult
from pipeline.crawl import Page, Tools, find_link, get_page, links
from pipeline.db import now
from pipeline.http import Fetcher
from pipeline.normalize import registrable_domain

STAGE = "crawl_more"
KINDS = [("team", "team_words"), ("jobs", "jobs_words"), ("references", "references_words")]


def company_sites(ctx: Ctx) -> list[tuple[str, str, str]]:
    """(company_id, domain, home url) for confirmed companies."""
    return ctx.con.execute(
        """SELECT co.company_id, s.domain, s.final_url
           FROM company co JOIN crawl_site s ON s.final_url = co.website AND s.status = 'ok'
           WHERE co.tab = 'companies' AND co.status = 'confirmed'
           QUALIFY row_number() OVER (PARTITION BY co.company_id ORDER BY s.domain) = 1
           ORDER BY co.company_id"""
    ).fetchall()


async def _one(t: Tools, domain: str, home_url: str, cfg: dict, known: set[str]) -> tuple[dict[str, Page], str | None]:
    home = await get_page(t, home_url)
    if isinstance(home, str):
        return {}, home
    found = links(home.html, home.final_url)
    site = registrable_domain(home.final_url)
    used = set(known) | {home.final_url}
    pages: dict[str, Page] = {}
    for kind, key in KINDS:
        u = find_link(found, cfg[key], site, used)
        if not u:
            continue
        used.add(u)
        p = await get_page(t, u)
        if isinstance(p, Page):
            pages[kind] = p
    return pages, None


async def _all(ctx: Ctx, sites: list[tuple[str, str, str]], known: dict[str, set[str]]) -> tuple[list, Tools]:
    cc, cm = ctx.cfg["crawl"], ctx.cfg["crawl_more"]
    async with Fetcher() as f:
        t = Tools(f, cc["min_text_chars"], asyncio.Semaphore(cc["playwright_concurrency"]),
                  cc["playwright_timeout_s"] * 1000, None)   # no Tavily in Task 2
        try:
            results = await asyncio.gather(*(_one(t, d, u, cm, known.get(d, set())) for _, d, u in sites))
        finally:
            if t.browser is not None:
                await t.browser.close()
            if t._pw is not None:
                await t._pw.stop()
        t.stats["http_requests"] = f.requests
    return results, t


def run_crawl_more(ctx: Ctx, params: dict[str, Any] | None = None) -> StageResult:
    res = StageResult(run_id=ctx.run_id, stage=STAGE)
    sites = company_sites(ctx)
    have = ctx.con.execute("SELECT domain, url, page_type FROM page").fetchall()
    known: dict[str, set[str]] = {}
    done_kinds: dict[str, set[str]] = {}
    for d, u, k in have:
        known.setdefault(d, set()).add(u)
        done_kinds.setdefault(d, set()).add(k)
    if ctx.mode == "incremental":
        sites = [s for s in sites if not done_kinds.get(s[1], set()) & {"team", "jobs", "references"}]
    print(f"[crawl_more] {len(sites)} confirmed companies")
    results, t = asyncio.run(_all(ctx, sites, known))

    ts = now()
    kinds: Counter[str] = Counter()
    failed: Counter[str] = Counter()
    rows = []
    for (_, domain, _), (pages, err) in zip(sites, results):
        if err:
            failed[err] += 1
        for kind, p in pages.items():
            kinds[kind] += 1
            rows.append([p.url, domain, kind, p.status, p.text, p.via, ts])
    if rows:
        ctx.con.executemany("INSERT OR REPLACE INTO page VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    res.counts = {
        "companies": len(sites),
        "companies_with_new_pages": sum(1 for p, _ in results if p),
        "pages_by_type": {k: kinds[k] for k, _ in KINDS},
        "pages_new": len(rows),
        "home_failed": dict(failed),
        "pages_via": {k: t.stats[k] for k in ("httpx", "playwright")},
        "http_requests": t.stats["http_requests"],
    }
    for k, v in res.counts.items():
        ctx.log(STAGE, k, v)
    return res.finish("ok")
