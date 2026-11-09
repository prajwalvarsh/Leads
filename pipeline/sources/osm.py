"""OpenStreetMap via Overpass: one query for Berlin. Data © OpenStreetMap contributors."""
from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from pipeline.config import USER_AGENT
from pipeline.models import Candidate
from pipeline.normalize import clean_ws, phone_e164

OVERPASS = "https://overpass-api.de/api/interpreter"

QUERY = """
[out:json][timeout:180];
area["name"="Berlin"]["admin_level"="4"]->.b;
(
  nwr(area.b)["office"="property_management"];
  nwr(area.b)["name"~"Hausverwaltung|Immobilienverwaltung|WEG-Verwaltung",i];
);
out center tags;
"""


def parse(elements: list[dict]) -> list[Candidate]:
    out: list[Candidate] = []
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        street = clean_ws(f"{tags.get('addr:street', '')} {tags.get('addr:housenumber', '')}") or None
        lat = el.get("lat", el.get("center", {}).get("lat"))
        lon = el.get("lon", el.get("center", {}).get("lon"))
        phone_raw = tags.get("phone") or tags.get("contact:phone")
        category = next(
            (f"{k}={tags[k]}" for k in ("office", "shop", "amenity", "craft") if k in tags), None
        )
        out.append(
            Candidate(
                source="osm",
                source_ref=f"{el['type']}/{el['id']}",
                name=clean_ws(name),
                street=street,
                postcode=tags.get("addr:postcode"),
                city=tags.get("addr:city"),
                phone=phone_e164(phone_raw.split(";")[0]) if phone_raw else None,
                website=tags.get("website") or tags.get("contact:website"),
                lat=lat,
                lon=lon,
                raw_category=category,
            )
        )
    return out


def _temporary(e: BaseException) -> bool:
    """Overpass is often briefly overloaded (429/502/503/504) or slow; those are worth a retry."""
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code in (429, 502, 503, 504)
    return isinstance(e, httpx.TransportError)


@retry(retry=retry_if_exception(_temporary), stop=stop_after_attempt(3),
       wait=wait_exponential(multiplier=15, max=60), reraise=True)
def discover() -> list[Candidate]:
    r = httpx.post(
        OVERPASS, data={"data": QUERY}, headers={"User-Agent": USER_AGENT}, timeout=200
    )
    r.raise_for_status()
    return parse(r.json()["elements"])
