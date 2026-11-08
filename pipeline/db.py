"""DuckDB connection, schema and run-log helpers."""
from __future__ import annotations

from datetime import datetime, timezone

import duckdb

from pipeline.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidate (
    candidate_id VARCHAR PRIMARY KEY,
    source VARCHAR NOT NULL,          -- places, osm, gelbeseiten, dasoertliche, 11880, search, association
    source_ref VARCHAR NOT NULL,      -- id/URL within the source (place_id for places)
    name VARCHAR, street VARCHAR, postcode VARCHAR, city VARCHAR, phone VARCHAR,
    website VARCHAR, lat DOUBLE, lon DOUBLE, raw_category VARCHAR,
    fetched_at TIMESTAMP
);
-- Transient Google Places fields. Dropped at end of run (export stage).
CREATE TABLE IF NOT EXISTS places_tmp (
    place_id VARCHAR PRIMARY KEY, name VARCHAR, formatted_address VARCHAR,
    website VARCHAR, lat DOUBLE, lon DOUBLE
);
-- Transient Google Maps fields obtained via Serper. Dropped at end of run (export stage).
CREATE TABLE IF NOT EXISTS serper_maps_tmp (
    ref VARCHAR PRIMARY KEY, name VARCHAR, address VARCHAR, website VARCHAR, phone VARCHAR,
    lat DOUBLE, lon DOUBLE, type VARCHAR
);
CREATE TABLE IF NOT EXISTS place_ref (
    candidate_id VARCHAR, place_id VARCHAR, PRIMARY KEY (candidate_id, place_id)
);
CREATE TABLE IF NOT EXISTS page (
    url VARCHAR PRIMARY KEY, domain VARCHAR, page_type VARCHAR, status INTEGER,
    text VARCHAR, fetched_via VARCHAR, fetched_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS fact (
    candidate_id VARCHAR, field VARCHAR, value VARCHAR, evidence_quote VARCHAR,
    source_url VARCHAR, method VARCHAR
);
CREATE TABLE IF NOT EXISTS excluded (candidate_id VARCHAR, reason VARCHAR);
CREATE TABLE IF NOT EXISTS run_log (
    run_id VARCHAR, stage VARCHAR, key VARCHAR, value VARCHAR, ts TIMESTAMP
);
-- Paid-API usage per calendar month (caps are enforced per run AND per month).
CREATE TABLE IF NOT EXISTS api_usage (
    month VARCHAR, api VARCHAR, calls INTEGER, PRIMARY KEY (month, api)
);
-- Adaptive-grid log for Places Text Search (our own tile geometry + counts only).
CREATE TABLE IF NOT EXISTS places_tile (
    run_id VARCHAR, query VARCHAR, depth INTEGER,
    south DOUBLE, west DOUBLE, north DOUBLE, east DOUBLE,
    pages INTEGER, results INTEGER, operational INTEGER, split BOOLEAN
);
-- Portal pages from search that list many property managers (for LLM extraction).
CREATE TABLE IF NOT EXISTS portal_url (
    url VARCHAR PRIMARY KEY, domain VARCHAR, query VARCHAR, title VARCHAR, fetched_at TIMESTAMP
);
"""


GOOGLE_TMP_TABLES = ("places_tmp", "serper_maps_tmp")


def drop_google_tmp(con: duckdb.DuckDBPyConnection) -> None:
    """End of run: remove every transient Google field (CLAUDE.md rule 1)."""
    for t in GOOGLE_TMP_TABLES:
        con.execute(f"DROP TABLE IF EXISTS {t}")


def connect() -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA)
    return con


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def log(con: duckdb.DuckDBPyConnection, run_id: str, stage: str, key: str, value: object) -> None:
    con.execute(
        "INSERT INTO run_log VALUES (?, ?, ?, ?, ?)", [run_id, stage, key, str(value), now()]
    )
