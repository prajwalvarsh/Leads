"""Stage 3: crawl each resolved website: home, Impressum, and up to 2 pages for classification.

- httpx via the polite Fetcher (robots.txt, 1 req/s per host, 20 hosts in parallel, disk cache).
- Impressum: link by anchor text or path (impressum, imprint, rechtliches, anbieterkennzeichnung),
  else the fallback paths, else the home page itself if it carries the Impressum.
- At most 2 more pages: about / über uns and leistungen / services.
- Text: trafilatura, fallback selectolax. Less than crawl.min_text_chars -> Playwright render
  -> still short -> Tavily Extract (stage budget). `fetched_via` records which one worked.
- One site per registrable domain from the `website` table. Results: `page` + `crawl_site`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urljoin, urlparse

import trafilatura
from selectolax.lexbor import LexborHTMLParser

from pipeline.budget import Budget, CapExceeded
from pipeline.config import env
from pipeline.contract import Ctx, StageResult
from pipeline.db import now
from pipeline.http import HTTP_CACHE, Fetcher, Response, Skip
from pipeline.normalize import clean_ws, fold, registrable_domain, site_root

STAGE = "crawl"
IMPRESSUM_MARKERS = re.compile(r"impressum|angaben gem(ä|ae)ss|§\s*5\s*(tmg|ddg)|anbieterkennzeichnung|imprint", re.I)


# ---------- text and links ----------
def html_text(html: str, url: str | None, min_chars: int) -> str:
    """trafilatura first; selectolax body text if trafilatura returns too little."""
    t = ""
    try:
        t = trafilatura.extract(html, url=url, favor_recall=True, include_tables=True,
                                include_comments=False) or ""
    except Exception:  # trafilatura can fail on broken markup
        t = ""
    if len(t) >= min_chars:
        return t.strip()
    tree = LexborHTMLParser(html)
    for sel in ("script", "style", "noscript", "svg", "template"):
        for n in tree.css(sel):
            n.decompose()
    body = tree.body
    raw = body.text(separator="\n") if body else ""
    lines = [clean_ws(x) for x in raw.splitlines()]
    s = "\n".join(x for x in lines if x)
    return s if len(s) > len(t) else t.strip()


def links(html: str, base: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for a in LexborHTMLParser(html).css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base, href).split("#")[0]
        if url.startswith("http"):
            out.append((url, clean_ws(a.text(separator=" "))))
    return out


def same_site(url: str, domain: str | None) -> bool:
    return bool(domain) and registrable_domain(url) == domain


def find_link(found: list[tuple[str, str]], words: list[str], domain: str | None,
              skip: set[str] = frozenset()) -> str | None:
    """Anchor-text match first, then URL-path match; same registrable domain only."""
    ws = [fold(w) for w in words]
    cands = [(u, fold(t)) for u, t in found if same_site(u, domain) and u not in skip]
    for u, t in cands:
        if any(w in t for w in ws):
            return u
    for u, _ in cands:
        path = fold(urlparse(u).path)
        if any(w.replace(" ", "-") in path or w.replace(" ", "") in path for w in ws):
            return u
    return None


def looks_like_impressum(text: str) -> bool:
    return bool(IMPRESSUM_MARKERS.search(text))


# ---------- page fetching with fallbacks ----------
@dataclass
class Page:
    url: str
    final_url: str
    status: int
    text: str
    html: str
    via: str


@dataclass
class Tools:
    fetcher: Fetcher
    min_chars: int
    pw_sem: asyncio.Semaphore
    pw_timeout_ms: int
    tavily_budget: Budget | None
    browser: Any = None          # playwright browser, started lazily
    pw_failed: str | None = None
    stats: dict[str, int] = field(default_factory=lambda: {"httpx": 0, "playwright": 0, "tavily": 0,
                                                           "playwright_renders": 0, "tavily_calls": 0})
    _pw_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _pw: Any = None


def _cache(kind: str, url: str) -> Any:
    return HTTP_CACHE / f"{kind}_{hashlib.sha1(url.encode()).hexdigest()}.json"


async def _browser(t: Tools) -> Any:
    async with t._pw_lock:
        if t.browser is None and t.pw_failed is None:
            try:
                from playwright.async_api import async_playwright
                t._pw = await async_playwright().start()
                t.browser = await t._pw.chromium.launch()
            except Exception as e:   # browser not installed etc.: continue without it
                t.pw_failed = f"{type(e).__name__}: {e}"[:200]
    return t.browser


async def render(t: Tools, url: str) -> str | None:
    """Playwright fallback for JS-only pages. Same robots and per-host rate limit as httpx."""
    path = _cache("pw", url)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["html"]
    if not t.fetcher.allowed(url):
        return None
    browser = await _browser(t)
    if browser is None:
        return None
    from pipeline.config import USER_AGENT
    h = t.fetcher._host(urlparse(url).hostname or "")
    async with t.pw_sem:
        async with h.lock:
            await t.fetcher._wait_turn(h)
        ctx = await browser.new_context(user_agent=USER_AGENT, locale="de-DE")
        try:
            page = await ctx.new_page()
            await page.route("**/*", lambda r: r.abort() if r.request.resource_type in
                             ("image", "media", "font") else r.continue_())
            await page.goto(url, timeout=t.pw_timeout_ms, wait_until="load")
            await page.wait_for_timeout(1500)
            html = await page.content()
            t.stats["playwright_renders"] += 1
        except Exception:
            return None
        finally:
            await ctx.close()
    path.write_text(json.dumps({"url": url, "html": html}), encoding="utf-8")
    return html


async def tavily_extract(t: Tools, url: str) -> str | None:
    path = _cache("tavily", url)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["text"]
    if t.tavily_budget is None or t.tavily_budget.remaining() < 1:
        return None
    t.tavily_budget.spend(1)
    t.stats["tavily_calls"] += 1
    try:
        from tavily import TavilyClient
        data = await asyncio.to_thread(TavilyClient(env("TAVILY_API_KEY")).extract, urls=[url], format="text")
    except Exception:
        return None
    res = data.get("results") or []
    text = (res[0].get("raw_content") or "") if res else ""
    path.write_text(json.dumps({"url": url, "text": text}), encoding="utf-8")
    return text


async def get_page(t: Tools, url: str) -> Page | str:
    """Page with text, or a failure reason."""
    r = await t.fetcher.fetch(url)
    if isinstance(r, Skip):
        return r.reason
    assert isinstance(r, Response)
    if r.status >= 400:
        return f"http_{r.status}"
    if r.content_type and "html" not in r.content_type.lower():
        return "not_html"
    page = Page(url, r.final_url, r.status, html_text(r.text, r.final_url, t.min_chars), r.text, "httpx")
    if len(page.text) < t.min_chars:
        html = await render(t, r.final_url)
        if html:
            text = html_text(html, r.final_url, t.min_chars)
            if len(text) > len(page.text):
                page.text, page.html, page.via = text, html, "playwright"
    if len(page.text) < t.min_chars:
        try:
            text = await tavily_extract(t, r.final_url)
        except CapExceeded:
            text = None
        if text and len(text) > len(page.text):
            page.text, page.via = text, "tavily"
    t.stats[page.via] += 1
    return page


# ---------- one site ----------
@dataclass
class SiteResult:
    domain: str
    start_url: str
    status: str = "failed"
    reason: str | None = None
    final_url: str | None = None
    impressum_url: str | None = None
    found_by: str | None = None
    pages: dict[str, Page] = field(default_factory=dict)


async def crawl_site(t: Tools, domain: str, start_url: str, cc: dict) -> SiteResult:
    res = SiteResult(domain, start_url)
    home = await get_page(t, start_url)
    if isinstance(home, str):
        res.reason = home
        return res
    res.status, res.final_url = "ok", home.final_url
    res.pages["home"] = home
    site_dom = registrable_domain(home.final_url)
    found = links(home.html, home.final_url)

    imp_url = find_link(found, cc["impressum_words"], site_dom)
    if imp_url and imp_url.rstrip("/") != home.final_url.rstrip("/"):
        p = await get_page(t, imp_url)
        if isinstance(p, Page):
            res.pages["impressum"], res.impressum_url, res.found_by = p, imp_url, "link"
    if "impressum" not in res.pages:
        root = site_root(home.final_url).rstrip("/")
        for path in cc["fallback_paths"]:
            p = await get_page(t, root + path)
            # many sites answer unknown paths with the home page: require a real Impressum
            if isinstance(p, Page) and urlparse(p.final_url).path not in ("", "/") \
                    and looks_like_impressum(p.text):
                res.pages["impressum"], res.impressum_url, res.found_by = p, root + path, f"path:{path}"
                break
    if "impressum" not in res.pages and looks_like_impressum(home.text):
        res.impressum_url, res.found_by = home.final_url, "home"   # one-page sites

    used = {home.final_url, res.impressum_url or ""}
    for kind, words in (("about", cc["about_words"]), ("services", cc["services_words"])):
        u = find_link(found, words, site_dom, used)
        if u:
            used.add(u)
            p = await get_page(t, u)
            if isinstance(p, Page):
                res.pages[kind] = p
    if "impressum" not in res.pages and res.found_by != "home":
        res.reason = "no_impressum"
    return res


# ---------- stage ----------
def sites_to_crawl(ctx: Ctx) -> list[tuple[str, str]]:
    rows = ctx.con.execute(
        """SELECT domain, min(url) FROM website WHERE url IS NOT NULL AND domain IS NOT NULL
           GROUP BY domain ORDER BY domain"""
    ).fetchall()
    if ctx.mode == "incremental":
        cutoff = now() - timedelta(days=ctx.cfg["crawl"]["max_age_days"])
        fresh = {r[0] for r in ctx.con.execute(
            "SELECT domain FROM crawl_site WHERE crawled_at >= ?", [cutoff]).fetchall()}
        rows = [r for r in rows if r[0] not in fresh]
    return [(d, u if u.startswith("http") else "http://" + u) for d, u in rows]


async def _crawl_all(ctx: Ctx, sites: list[tuple[str, str]], tavily_budget: Budget | None) -> tuple[list[SiteResult], Tools]:
    cc = ctx.cfg["crawl"]
    async with Fetcher() as f:
        t = Tools(f, cc["min_text_chars"], asyncio.Semaphore(cc["playwright_concurrency"]),
                  cc["playwright_timeout_s"] * 1000, tavily_budget)
        done = 0

        async def one(d: str, u: str) -> SiteResult:
            nonlocal done
            try:
                r = await crawl_site(t, d, u, cc)
            except Exception as e:   # one broken site must not stop the crawl
                r = SiteResult(d, u, reason=f"error:{type(e).__name__}")
            done += 1
            if done % 100 == 0:
                print(f"[crawl] {done}/{len(sites)} sites, {f.requests} requests")
            return r

        try:
            results = await asyncio.gather(*(one(d, u) for d, u in sites))
        finally:
            if t.browser is not None:
                await t.browser.close()
            if t._pw is not None:
                await t._pw.stop()
        t.stats["http_requests"] = f.requests
        t.stats["http_skips"] = len(f.skips)
    return results, t


def run_crawl(ctx: Ctx, params: dict[str, Any] | None = None) -> StageResult:
    params = params or {}
    res = StageResult(run_id=ctx.run_id, stage=STAGE)
    sites = sites_to_crawl(ctx)
    if params.get("limit"):   # trial runs on the first N sites
        sites = sites[: int(params["limit"])]
    tavily_budget = None
    if env("TAVILY_API_KEY"):
        tavily_budget = Budget(ctx.con, "tavily", ctx.cfg["tavily_budgets"]["crawl"], ctx.cfg["caps"]["tavily_credits"])
    print(f"[crawl] {len(sites)} sites ({ctx.mode})")
    results, t = asyncio.run(_crawl_all(ctx, sites, tavily_budget))

    ts = now()
    for r in results:
        ctx.con.execute("DELETE FROM page WHERE domain = ?", [r.domain])
        if r.pages:
            ctx.con.executemany(
                "INSERT OR REPLACE INTO page VALUES (?, ?, ?, ?, ?, ?, ?)",
                [[p.url, r.domain, kind, p.status, p.text, p.via, ts] for kind, p in r.pages.items()],
            )
        ctx.con.execute(
            "INSERT OR REPLACE INTO crawl_site VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [r.domain, r.start_url, r.final_url, r.status, r.reason, r.impressum_url, r.found_by,
             len(r.pages), ctx.run_id, ts],
        )

    ok = [r for r in results if r.status == "ok"]
    failed: dict[str, int] = {}
    for r in results:
        if r.status != "ok":
            failed[r.reason or "unknown"] = failed.get(r.reason or "unknown", 0) + 1
    found_by: dict[str, int] = {}
    for r in ok:
        k = (r.found_by or "none").split(":")[0]
        found_by[k] = found_by.get(k, 0) + 1
    pages_by_type: dict[str, int] = {}
    for r in ok:
        for k in r.pages:
            pages_by_type[k] = pages_by_type.get(k, 0) + 1
    res.counts = {
        "sites": len(results),
        "sites_fetched": len(ok),
        "failed": len(results) - len(ok),
        "failed_by_reason": dict(sorted(failed.items(), key=lambda x: -x[1])),
        "impressum_found": sum(1 for r in ok if r.impressum_url),
        "impressum_found_by": found_by,
        "pages_by_type": pages_by_type,
        "pages_via": {k: t.stats[k] for k in ("httpx", "playwright", "tavily")},
        "playwright_renders": t.stats["playwright_renders"],
        "tavily_calls": t.stats["tavily_calls"],
        "http_requests": t.stats["http_requests"],
        "http_skips": t.stats["http_skips"],
    }
    if t.pw_failed:
        res.errors.append(f"playwright unavailable: {t.pw_failed}")
    res.api_calls["tavily_credits"] = tavily_budget.used if tavily_budget else 0
    for k, v in res.counts.items():
        ctx.log(STAGE, k, v)
    return res.finish("failed" if res.errors else "ok")
