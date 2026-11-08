"""Web search (Tavily) for Hausverwaltungen per Bezirk and Ortsteil.

Company-looking domains become candidates (source 'search', one per registrable domain).
Portal/directory pages are stored in portal_url for LLM list extraction.
Shared hosts (jimdo, facebook, ...) are dropped.
"""
from __future__ import annotations

import duckdb
from tavily import TavilyClient

from pipeline.budget import Budget, CapExceeded
from pipeline.config import cfg, env, ortsteile
from pipeline.db import now
from pipeline.models import Candidate
from pipeline.normalize import clean_ws, is_listed_domain, registrable_domain, site_root


def queries() -> list[str]:
    t = cfg()["search_templates"]
    out = [q.format(bezirk=b) for b in cfg()["bezirke"] for q in t["bezirk"]]
    out += [q.format(ortsteil=o) for _, o in ortsteile() for q in t["ortsteil"]]
    return out


def classify_url(url: str) -> str:
    """'company', 'portal' or 'shared'."""
    d = registrable_domain(url)
    if not d or is_listed_domain(d, cfg()["shared_hosts"]):
        return "shared"
    if is_listed_domain(d, cfg()["portal_domains"]):
        return "portal"
    return "company"


def tavily_search(client: TavilyClient, budget: Budget, query: str) -> list[dict]:
    budget.spend(1)  # basic search = 1 credit
    res = client.search(query=query, search_depth="basic", max_results=20)
    return res.get("results", [])


def discover(con: duckdb.DuckDBPyConnection, budget: Budget) -> tuple[list[Candidate], dict]:
    key = env("TAVILY_API_KEY")
    if not key:
        raise RuntimeError("TAVILY_API_KEY missing in .env")
    client = TavilyClient(api_key=key)
    stats = {"queries": 0, "results": 0, "company": 0, "portal": 0, "shared": 0}
    by_domain: dict[str, Candidate] = {}
    for q in queries():
        try:
            results = tavily_search(client, budget, q)
        except CapExceeded as e:
            stats["stopped"] = str(e)
            break
        stats["queries"] += 1
        stats["results"] += len(results)
        for r in results:
            url = r.get("url") or ""
            kind = classify_url(url)
            stats[kind] += 1
            if kind == "portal":
                con.execute(
                    "INSERT OR IGNORE INTO portal_url VALUES (?, ?, ?, ?, ?)",
                    [url, registrable_domain(url), q, clean_ws(r.get("title")), now()],
                )
            elif kind == "company":
                d = registrable_domain(url)
                by_domain.setdefault(
                    d,
                    Candidate(
                        source="search", source_ref=d, name=clean_ws(r.get("title")) or None,
                        website=site_root(url), raw_category=f"search: {q}",
                    ),
                )
    return list(by_domain.values()), stats
