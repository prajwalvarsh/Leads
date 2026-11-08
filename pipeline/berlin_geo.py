"""Berlin geometry: Bezirk polygons, Berlin outline, postcode check."""
from __future__ import annotations

import json
from functools import lru_cache

from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.prepared import prep

from pipeline.config import DATA

BEZIRKE_PATH = DATA / "geo" / "bezirke.geojson"


@lru_cache
def bezirke() -> dict[str, BaseGeometry]:
    with open(BEZIRKE_PATH, encoding="utf-8") as f:
        gj = json.load(f)
    return {ft["properties"]["bezirk"]: shape(ft["geometry"]) for ft in gj["features"]}


@lru_cache
def berlin() -> BaseGeometry:
    return unary_union(list(bezirke().values()))


@lru_cache
def _berlin_prepared():  # noqa: ANN202
    return prep(berlin())


def in_berlin(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    return _berlin_prepared().contains(Point(lon, lat))


def bezirk_of(lat: float, lon: float) -> str | None:
    p = Point(lon, lat)
    for name, poly in bezirke().items():
        if poly.contains(p):
            return name
    return None


def is_berlin_postcode(postcode: str | None) -> bool:
    return bool(postcode and len(postcode) == 5 and postcode.isdigit() and "10115" <= postcode <= "14199")
