"""Serper Maps: Google Maps results via google.serper.dev/maps.

Google data rule (CLAUDE.md #1): only the CID / place ID is persisted (candidate.source_ref,
place_ref). Everything else goes to serper_maps_tmp, dropped at the end of the run.

The response parser is written from one real saved response
(tests/fixtures/serper_maps_sample.json); see parse_places().
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

import duckdb
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from pipeline.berlin_geo import berlin, bezirke, in_berlin, is_berlin_postcode
from pipeline.budget import Budget
from pipeline.config import ROOT, env
from pipeline.db import now
from pipeline.models import candidate_id

URL = "https://google.serper.dev/maps"
SEARCH_URL = "https://google.serper.dev/search"
SAMPLE_PATH = ROOT / "tests" / "fixtures" / "serper_maps_sample.json"
KM_PER_DEG_LAT = 111.32


@dataclass(frozen=True)
class GridPoint:
    lat: float
    lon: float

    def ll(self, zoom: int) -> str:
        return f"@{self.lat:.6f},{self.lon:.6f},{zoom}z"


def grid_points(spacing_km: float) -> list[GridPoint]:
    """Regular grid `spacing_km` apart, kept only if inside Berlin."""
    minx, miny, maxx, maxy = berlin().bounds  # lon/lat
    dlat = spacing_km / KM_PER_DEG_LAT
    dlon = spacing_km / (KM_PER_DEG_LAT * math.cos(math.radians((miny + maxy) / 2)))
    pts: list[GridPoint] = []
    lat = miny + dlat / 2
    while lat < maxy:
        lon = minx + dlon / 2
        while lon < maxx:
            if in_berlin(lat, lon):
                pts.append(GridPoint(round(lat, 6), round(lon, 6)))
            lon += dlon
        lat += dlat
    return pts


PILOT_BEZIRKE = ["Mitte", "Spandau", "Pankow", "Treptow-Köpenick", "Steglitz-Zehlendorf"]


def pilot_points(n: int = 5) -> list[GridPoint]:
    """n points spread across Berlin: a point inside each of n different Bezirke."""
    out = []
    for name in PILOT_BEZIRKE[:n]:
        p = bezirke()[name].representative_point()
        out.append(GridPoint(round(p.y, 6), round(p.x, 6)))
    return out


def should_fetch_next(page: int, results: int, new_places: int, max_pages: int, min_new: int) -> bool:
    """After fetching `page`: continue only if it had results, added >= min_new unseen
    places, and max_pages is not reached."""
    return results > 0 and new_places >= min_new and page < max_pages


class SerperClient:
    def __init__(self, budget: Budget) -> None:
        key = env("SERPER_API_KEY")
        if not key:
            raise RuntimeError("SERPER_API_KEY missing in .env")
        self.http = httpx.Client(headers={"X-API-KEY": key, "Content-Type": "application/json"}, timeout=30)
        self.budget = budget
        self.calls = 0
        self.credits = 0

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, max=20),
        reraise=True,
    )
    def _post(self, url: str, body: dict) -> httpx.Response:
        return self.http.post(url, json=body)

    def maps(self, q: str, point: GridPoint, page: int, zoom: int) -> dict:
        return self._call(URL, {"q": q, "ll": point.ll(zoom), "gl": "de", "hl": "de", "page": page})

    def search(self, q: str) -> dict:
        """Google web search via Serper (1 credit in the saved sample)."""
        return self._call(SEARCH_URL, {"q": q, "gl": "de", "hl": "de"})

    def _call(self, url: str, body: dict) -> dict:
        self.budget.spend(1)  # reserve 1 credit; corrected below if the response reports more
        r = self._post(url, body)
        self.calls += 1
        if r.status_code >= 400:
            raise RuntimeError(f"Serper error {r.status_code}: {r.text[:300]}")
        data = r.json()
        used = int(data.get("credits", 1) or 1)
        if used > 1:
            self.budget.spend(used - 1)
        self.credits += used
        return data


@dataclass
class Place:
    """Transient Google Maps fields (only `ref` may be persisted)."""
    ref: str
    name: str | None
    address: str | None
    website: str | None
    phone: str | None
    lat: float | None
    lon: float | None
    type: str | None


def parse_places(data: dict) -> list[Place]:
    """Map one Serper Maps response to Place objects (fields as in tests/fixtures/serper_maps_sample.json).

    ref = Google CID (always present in the sample); placeId as fallback. Places without either are skipped.
    """
    out: list[Place] = []
    for p in data.get("places") or []:
        ref = p.get("cid") or p.get("placeId")
        if not ref:
            continue
        out.append(Place(
            ref=str(ref),
            name=p.get("title"),
            address=p.get("address"),
            website=p.get("website"),
            phone=p.get("phoneNumber"),
            lat=p.get("latitude"),
            lon=p.get("longitude"),
            type=p.get("type"),
        ))
    return out


_PLZ = re.compile(r"\b(\d{5})\b")


def is_berlin_place(p: Place) -> bool:
    """Berlin postcode in the address, or coordinates inside Berlin."""
    m = _PLZ.findall(p.address or "")
    if any(is_berlin_postcode(x) for x in m):
        return True
    return in_berlin(p.lat, p.lon)


@dataclass
class SweepStats:
    calls: int = 0
    credits: int = 0
    places_returned: int = 0
    unique_places: int = 0
    kept_berlin: int = 0
    dropped_not_berlin: int = 0
    new_per_query: dict[str, int] | None = None
    new_per_call: list[int] | None = None
    credits_per_point: list[int] | None = None  # summed over all queries


def sweep(
    con: duckdb.DuckDBPyConnection, client: SerperClient, points: list[GridPoint], sm_cfg: dict,
    store: bool = True,
) -> SweepStats:
    """All queries x points with the pagination stop rule. Stores Berlin places if store=True."""
    seen: set[str] = set()
    st = SweepStats(new_per_query={}, new_per_call=[], credits_per_point=[0] * len(points))
    for q in sm_cfg["queries"]:
        st.new_per_query[q] = 0
        for i, pt in enumerate(points):
            before = client.credits
            page = 1
            while True:
                data = client.maps(q, pt, page, sm_cfg["zoom"])
                places = parse_places(data)
                new = [p for p in places if p.ref not in seen]
                seen.update(p.ref for p in new)
                st.places_returned += len(places)
                st.new_per_call.append(len(new))
                st.new_per_query[q] += len(new)
                for p in new:
                    if is_berlin_place(p):
                        st.kept_berlin += 1
                        if store:
                            store_place(con, p)
                    else:
                        st.dropped_not_berlin += 1
                if not should_fetch_next(page, len(places), len(new), sm_cfg["max_pages"],
                                         sm_cfg["min_new_per_page"]):
                    break
                page += 1
            st.credits_per_point[i] += client.credits - before
    st.calls, st.credits, st.unique_places = client.calls, client.credits, len(seen)
    return st


def store_place(con: duckdb.DuckDBPyConnection, p: Place) -> None:
    con.execute(
        "INSERT OR REPLACE INTO serper_maps_tmp VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [p.ref, p.name, p.address, p.website, p.phone, p.lat, p.lon, p.type],
    )
    cid = candidate_id("serper_maps", p.ref)
    # Persistent row: only the Google ref, nothing else from Google.
    con.execute(
        """INSERT OR REPLACE INTO candidate (candidate_id, source, source_ref, fetched_at)
           VALUES (?, 'serper_maps', ?, ?)""",
        [cid, p.ref, now()],
    )
    con.execute("INSERT OR IGNORE INTO place_ref VALUES (?, ?)", [cid, p.ref])


SPACINGS_KM = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0]


def choose_spacing(credits_per_point: list[int], cap: int) -> tuple[float | None, dict[float, int]]:
    """Smallest spacing whose estimated full-run credits stay under the cap.

    Conservative estimate: grid points x credits of the busiest pilot point (every point is
    assumed to paginate as deeply as the busiest one).
    """
    worst = max(credits_per_point, default=0)
    estimates = {s: len(grid_points(s)) * worst for s in SPACINGS_KM}
    fitting = [s for s, est in estimates.items() if est <= cap]
    return (min(fitting) if fitting else None), estimates


def fetch_sample(budget: Budget, zoom: int = 14) -> dict:
    """ONE request (q=Hausverwaltung, Berlin-Mitte), saved raw as the parser fixture."""
    client = SerperClient(budget)
    mitte = bezirke()["Mitte"].representative_point()
    data = client.maps("Hausverwaltung", GridPoint(round(mitte.y, 6), round(mitte.x, 6)), 1, zoom)
    SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SAMPLE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
