"""Overlap between Serper Maps places and the candidates of another source.

A Serper place and a candidate overlap when they share the website domain, or the phone
number, or have a fuzzy name match >= 90 at the same street (and house number when both
have one). Serper fields come from serper_maps_tmp, so this only works during the run,
before the tmp table is dropped. Nothing here is stored; it only produces counts.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import duckdb
from rapidfuzz import fuzz

from pipeline.normalize import is_listed_domain, normalise_name, phone_e164, registrable_domain, split_street

NAME_MIN = 90


@dataclass
class Rec:
    key: str
    domain: str | None
    phone: str | None
    name: str
    street: tuple[str, str] | None


def _domain(url: str | None, excluded: list[str]) -> str | None:
    d = registrable_domain(url)
    return None if not d or is_listed_domain(d, excluded) else d


def _street(street: str | None) -> tuple[str, str] | None:
    if not street:
        return None
    name, num = split_street(street)
    return (name, num) if name else None


def serper_records(con: duckdb.DuckDBPyConnection, excluded: list[str]) -> list[Rec]:
    rows = con.execute("SELECT ref, name, address, website, phone FROM serper_maps_tmp").fetchall()
    # Serper address: "Immanuelkirchstraße 26, 10405 Berlin, Deutschland" -> street is the first part
    return [Rec(ref, _domain(web, excluded), phone_e164(phone), normalise_name(name),
                _street(addr.split(",")[0] if addr else None))
            for ref, name, addr, web, phone in rows]


def source_records(con: duckdb.DuckDBPyConnection, source: str, excluded: list[str]) -> list[Rec]:
    rows = con.execute(
        "SELECT candidate_id, name, street, website, phone FROM candidate WHERE source = ?", [source]
    ).fetchall()
    return [Rec(cid, _domain(web, excluded), phone_e164(phone), normalise_name(name), _street(street))
            for cid, name, street, web, phone in rows]


def _same_street(a: tuple[str, str], b: tuple[str, str]) -> bool:
    return a[0] == b[0] and (not a[1] or not b[1] or a[1] == b[1])


def match_reason(a: Rec, b: Rec) -> str | None:
    if a.domain and a.domain == b.domain:
        return "domain"
    if a.phone and a.phone == b.phone:
        return "phone"
    if a.name and b.name and a.street and b.street and _same_street(a.street, b.street) \
            and fuzz.token_set_ratio(a.name, b.name) >= NAME_MIN:
        return "name_street"
    return None


def overlap(serper: list[Rec], other: list[Rec]) -> dict:
    by_domain: dict[str, list[Rec]] = defaultdict(list)
    by_phone: dict[str, list[Rec]] = defaultdict(list)
    by_street: dict[str, list[Rec]] = defaultdict(list)
    for r in other:
        if r.domain:
            by_domain[r.domain].append(r)
        if r.phone:
            by_phone[r.phone].append(r)
        if r.street:
            by_street[r.street[0]].append(r)

    reasons: dict[str, int] = {"domain": 0, "phone": 0, "name_street": 0}
    matched_serper = 0
    matched_other: set[str] = set()
    for s in serper:
        pool = by_domain.get(s.domain or "", []) + by_phone.get(s.phone or "", []) \
            + by_street.get(s.street[0] if s.street else "", [])
        hits = [(o, match_reason(s, o)) for o in pool]
        hits = [(o, why) for o, why in hits if why]
        if hits:
            matched_serper += 1
            reasons[hits[0][1]] += 1  # first reason in priority order domain > phone > name_street
            matched_other.update(o.key for o, _ in hits)
    return {
        "serper_places": len(serper),
        "other_candidates": len(other),
        "serper_matched": matched_serper,
        "other_matched": len(matched_other),
        "serper_only": len(serper) - matched_serper,
        "other_only": len(other) - len(matched_other),
        "matched_by": reasons,
    }
