"""Optional manual list: data/manual/associations.csv (name, website, association)."""
from __future__ import annotations

import csv

from pipeline.config import DATA
from pipeline.models import Candidate
from pipeline.normalize import clean_ws, registrable_domain

PATH = DATA / "manual" / "associations.csv"


def discover() -> list[Candidate] | None:
    """None if the file does not exist."""
    if not PATH.exists():
        return None
    out: list[Candidate] = []
    with open(PATH, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            name = clean_ws(row.get("name"))
            website = clean_ws(row.get("website")) or None
            if not name:
                continue
            ref = registrable_domain(website) or name.lower()
            out.append(
                Candidate(
                    source="association", source_ref=ref, name=name, website=website,
                    raw_category=clean_ws(row.get("association")) or None,
                )
            )
    return out
