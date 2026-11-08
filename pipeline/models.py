"""Pydantic models shared by the stages."""
from __future__ import annotations

import hashlib
from datetime import datetime

from pydantic import BaseModel, Field

from pipeline.db import now


def candidate_id(source: str, source_ref: str) -> str:
    return hashlib.sha1(f"{source}|{source_ref}".encode()).hexdigest()[:16]


class Candidate(BaseModel):
    source: str
    source_ref: str
    name: str | None = None
    street: str | None = None
    postcode: str | None = None
    city: str | None = None
    phone: str | None = None        # E.164 when parseable
    website: str | None = None
    lat: float | None = None
    lon: float | None = None
    raw_category: str | None = None
    fetched_at: datetime = Field(default_factory=now)

    @property
    def candidate_id(self) -> str:
        return candidate_id(self.source, self.source_ref)

    def row(self) -> list:
        return [
            self.candidate_id, self.source, self.source_ref, self.name, self.street,
            self.postcode, self.city, self.phone, self.website, self.lat, self.lon,
            self.raw_category, self.fetched_at,
        ]
