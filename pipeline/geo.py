"""Stage 7: Berlin check, geocoding (Nominatim, cached) and Bezirk (point-in-polygon).

- Berlin check: postcode 10115-14199 AND city "Berlin" -> else tab "not_in_berlin".
  No postcode at all -> review "no address" (not proof of being outside Berlin).
- Nominatim: max 1 request/second, every result cached in `geocode` (also misses).
- Bezirk from data/geo/bezirke.geojson. No coordinates -> review "not geocoded".
"""
from __future__ import annotations

import re
import time
from typing import Any

import httpx

from pipeline.berlin_geo import bezirk_of, is_berlin_postcode
from pipeline.config import USER_AGENT
from pipeline.contract import Ctx, StageResult
from pipeline.db import now
from pipeline.normalize import fold

STAGE = "geo"


def is_berlin_address(postcode: str | None, city: str | None) -> bool:
    return is_berlin_postcode(postcode) and fold(city or "").startswith("berlin")


def clean_street(street: str) -> str:
    """'Geneststraße 5 (Zufahrt ..., 4. OG)' / 'Marzahner Chaussee 197, Zimmer 737' -> street + number only."""
    s = re.sub(r"\(.*?\)", " ", street)
    s = s.split(",")[0]
    return re.sub(r"\s+", " ", s).strip()


class Geocoder:
    def __init__(self, ctx: Ctx) -> None:
        self.con = ctx.con
        self.url = ctx.cfg["geo"]["nominatim_url"]
        self.http = httpx.Client(headers={"User-Agent": USER_AGENT, "Accept-Language": "de"}, timeout=30)
        self.last = 0.0
        self.calls = 0
        self.cache_hits = 0

    def geocode(self, street: str, postcode: str) -> tuple[float, float] | None:
        street = clean_street(street)
        query = f"{street}|{postcode}|Berlin"
        row = self.con.execute("SELECT lat, lon FROM geocode WHERE query = ?", [query]).fetchone()
        if row:
            self.cache_hits += 1
            return (row[0], row[1]) if row[0] is not None else None
        wait = self.last + 1.0 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        params = {"street": street, "postalcode": postcode, "city": "Berlin", "country": "Germany",
                  "format": "jsonv2", "limit": 1}
        try:
            r = self.http.get(self.url, params=params)
            self.calls += 1
            r.raise_for_status()
            hits = r.json()
        except (httpx.HTTPError, ValueError):
            return None   # not cached: a network error is not a "not found"
        lat = lon = display = None
        if hits:
            lat, lon, display = float(hits[0]["lat"]), float(hits[0]["lon"]), hits[0].get("display_name")
        self.con.execute("INSERT OR REPLACE INTO geocode VALUES (?, ?, ?, ?, ?)", [query, lat, lon, display, now()])
        return (lat, lon) if lat is not None else None


def _add_reason(current: str | None, reason: str) -> str:
    parts = [p for p in (current or "").split("; ") if p]
    return "; ".join(dict.fromkeys(parts + [reason]))


def run_geo(ctx: Ctx, params: dict[str, Any] | None = None) -> StageResult:
    res = StageResult(run_id=ctx.run_id, stage=STAGE)
    rows = ctx.con.execute(
        "SELECT company_id, street, postcode, city, status, review_reason FROM company WHERE tab = 'companies'"
    ).fetchall()
    g = Geocoder(ctx)
    in_berlin = not_berlin = not_geocoded = no_address = outside_polygon = 0
    updates = []
    for n, (cid, street, pc, city, status, reason) in enumerate(rows, 1):
        tab, bezirk, lat, lon = "companies", None, None, None
        if not pc:
            no_address += 1
            status, reason = "review", _add_reason(reason, "no address")
        elif not is_berlin_address(pc, city):
            not_berlin += 1
            tab = "not_in_berlin"
        else:
            in_berlin += 1
            hit = g.geocode(street, pc) if street else None
            if hit:
                lat, lon = hit
                bezirk = bezirk_of(lat, lon)
                if not bezirk:
                    outside_polygon += 1
                    status, reason = "review", _add_reason(reason, "geocoded outside Berlin")
            else:
                not_geocoded += 1
                status, reason = "review", _add_reason(reason, "not geocoded")
        updates.append([tab, bezirk, lat, lon, status, reason, cid])
        if n % 200 == 0:
            print(f"[geo] {n}/{len(rows)} companies, {g.calls} Nominatim calls, {g.cache_hits} cached")
    ctx.con.executemany(
        "UPDATE company SET tab = ?, bezirk = ?, lat = ?, lon = ?, status = ?, review_reason = ? WHERE company_id = ?",
        updates,
    )
    res.counts = {
        "companies_checked": len(rows),
        "in_berlin": in_berlin,
        "not_in_berlin": not_berlin,
        "not_geocoded": not_geocoded,
        "no_address": no_address,
        "geocoded_outside_bezirke": outside_polygon,
        "nominatim_calls": g.calls,
        "nominatim_cache_hits": g.cache_hits,
        "by_bezirk": dict(ctx.con.execute(
            "SELECT bezirk, count(*) FROM company WHERE tab = 'companies' AND bezirk IS NOT NULL GROUP BY 1 ORDER BY 1"
        ).fetchall()),
        "confirmed": ctx.con.execute("SELECT count(*) FROM company WHERE tab = 'companies' AND status LIKE 'confirmed%'").fetchone()[0],
        "review": ctx.con.execute("SELECT count(*) FROM company WHERE tab = 'companies' AND status = 'review'").fetchone()[0],
    }
    res.api_calls["nominatim"] = g.calls
    for k, v in res.counts.items():
        ctx.log(STAGE, k, v)
    return res.finish("ok")
