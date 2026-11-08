"""Google Places Text Search (New) with an adaptive grid over the Berlin bbox.

Only place_id is stored persistently (candidate.source_ref + place_ref). Name, address and
location go to places_tmp, which the export stage drops at the end of the run.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import duckdb
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from pipeline.budget import Budget
from pipeline.config import cfg, env
from pipeline.db import now

URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.businessStatus,nextPageToken"
)


@dataclass
class Tile:
    south: float
    west: float
    north: float
    east: float
    depth: int = 0

    def edge_m(self) -> float:
        """Shorter tile edge in metres."""
        lat_m = (self.north - self.south) * 111_320
        lon_m = (self.east - self.west) * 111_320 * math.cos(math.radians((self.north + self.south) / 2))
        return min(lat_m, lon_m)

    def split(self) -> list["Tile"]:
        mid_lat = (self.south + self.north) / 2
        mid_lon = (self.west + self.east) / 2
        d = self.depth + 1
        return [
            Tile(self.south, self.west, mid_lat, mid_lon, d),
            Tile(self.south, mid_lon, mid_lat, self.east, d),
            Tile(mid_lat, self.west, self.north, mid_lon, d),
            Tile(mid_lat, mid_lon, self.north, self.east, d),
        ]


def initial_tiles(n: int) -> list[Tile]:
    b = cfg()["bbox"]
    s, w, no, e = b["low"]["lat"], b["low"]["lon"], b["high"]["lat"], b["high"]["lon"]
    dlat, dlon = (no - s) / n, (e - w) / n
    return [
        Tile(s + i * dlat, w + j * dlon, s + (i + 1) * dlat, w + (j + 1) * dlon)
        for i in range(n)
        for j in range(n)
    ]


class PlacesClient:
    def __init__(self, budget: Budget) -> None:
        key = env("GOOGLE_PLACES_API_KEY")
        if not key:
            raise RuntimeError("GOOGLE_PLACES_API_KEY missing in .env")
        self.http = httpx.Client(
            headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELD_MASK}, timeout=30
        )
        self.budget = budget

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, max=20),
        reraise=True,
    )
    def _post(self, body: dict) -> dict:
        r = self.http.post(URL, json=body)
        if r.status_code == 429 or r.status_code >= 500:
            r.raise_for_status()  # retried
        if r.status_code >= 400:
            raise RuntimeError(f"Places error {r.status_code}: {r.text[:300]}")
        return r.json()

    def search_tile(self, query: str, tile: Tile) -> tuple[list[dict], int, bool]:
        """Up to 3 pages. Returns (places, pages_used, saturated)."""
        body = {
            "textQuery": query,
            "languageCode": "de",
            "regionCode": "DE",
            "pageSize": 20,
            "locationRestriction": {
                "rectangle": {
                    "low": {"latitude": tile.south, "longitude": tile.west},
                    "high": {"latitude": tile.north, "longitude": tile.east},
                }
            },
        }
        places: list[dict] = []
        pages = 0
        token = None
        while pages < 3:
            if token:
                body["pageToken"] = token
            self.budget.spend()  # every call counts, even if it fails
            data = self._post(body)
            pages += 1
            places += data.get("places", [])
            token = data.get("nextPageToken")
            if not token:
                break
        saturated = len(places) >= cfg()["places"]["max_results"]
        return places, pages, saturated


def discover(con: duckdb.DuckDBPyConnection, run_id: str, budget: Budget) -> dict:
    """Run the adaptive grid for every query; write places_tmp, candidate, place_ref."""
    pc = cfg()["places"]
    client = PlacesClient(budget)
    stats = {"tiles": 0, "calls": 0, "results": 0, "not_operational": 0}
    for query in pc["queries"]:
        queue = initial_tiles(pc["grid"])
        while queue:
            tile = queue.pop()
            places, pages, saturated = client.search_tile(query, tile)
            stats["tiles"] += 1
            stats["calls"] += pages
            stats["results"] += len(places)
            can_split = tile.edge_m() / 2 >= pc["min_tile_edge_m"]
            do_split = saturated and can_split
            operational = 0
            for p in places:
                if p.get("businessStatus") != "OPERATIONAL":
                    stats["not_operational"] += 1
                    continue
                operational += 1
                _store(con, p)
            con.execute(
                "INSERT INTO places_tile VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, query, tile.depth, tile.south, tile.west, tile.north, tile.east,
                 pages, len(places), operational, do_split],
            )
            if do_split:
                queue += tile.split()
            elif saturated:
                stats.setdefault("saturated_at_min_size", 0)
                stats["saturated_at_min_size"] += 1
    stats["unique_places"] = con.execute("SELECT count(*) FROM places_tmp").fetchone()[0]
    return stats


def _store(con: duckdb.DuckDBPyConnection, p: dict) -> None:
    from pipeline.models import candidate_id

    pid = p["id"]
    loc = p.get("location", {})
    con.execute(
        "INSERT OR REPLACE INTO places_tmp VALUES (?, ?, ?, NULL, ?, ?)",
        [pid, p.get("displayName", {}).get("text"), p.get("formattedAddress"),
         loc.get("latitude"), loc.get("longitude")],
    )
    cid = candidate_id("places", pid)
    # Persistent candidate row holds no Places fields except the place_id (as source_ref).
    con.execute(
        """INSERT OR REPLACE INTO candidate (candidate_id, source, source_ref, fetched_at)
           VALUES (?, 'places', ?, ?)""",
        [cid, pid, now()],
    )
    con.execute("INSERT OR IGNORE INTO place_ref VALUES (?, ?)", [cid, pid])
