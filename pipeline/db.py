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
-- resolve: one website per candidate. method: source | group | search | none (searched, no match).
-- from_google = the URL came from serper_maps_tmp (directly or via a group member).
CREATE TABLE IF NOT EXISTS website (
    candidate_id VARCHAR PRIMARY KEY, url VARCHAR, domain VARCHAR, method VARCHAR,
    detail VARCHAR, from_google BOOLEAN, run_id VARCHAR, resolved_at TIMESTAMP
);
-- crawl: one row per site (domain from the website table). status: ok | failed
CREATE TABLE IF NOT EXISTS crawl_site (
    domain VARCHAR PRIMARY KEY, start_url VARCHAR, final_url VARCHAR, status VARCHAR, reason VARCHAR,
    impressum_url VARCHAR, impressum_found_by VARCHAR, pages INTEGER, run_id VARCHAR, crawled_at TIMESTAMP
);
-- classify: one row per candidate. status: keep | drop | review. decided_by: llm+rules | llm | rules
CREATE TABLE IF NOT EXISTS classification (
    candidate_id VARCHAR PRIMARY KEY, status VARCHAR, decided_by VARCHAR, llm_verdict VARCHAR,
    services VARCHAR, is_housing_company BOOLEAN, quote VARCHAR, source_url VARCHAR,
    rule_verdict VARCHAR, rule_word VARCHAR, reason VARCHAR, run_id VARCHAR, classified_at TIMESTAMP
);
-- dedupe: one row per company (cluster). tab: companies | excluded | not_in_berlin (set by geo)
CREATE TABLE IF NOT EXISTS company (
    company_id VARCHAR PRIMARY KEY, primary_key VARCHAR, company_name VARCHAR, legal_form VARCHAR,
    street VARCHAR, postcode VARCHAR, city VARCHAR, bezirk VARCHAR, phone VARCHAR, website VARCHAR,
    impressum_url VARCHAR, register_number VARCHAR, sources VARCHAR, source_count INTEGER,
    google_place_id VARCHAR, status VARCHAR, review_reason VARCHAR, decided_by VARCHAR,
    excluded_reason VARCHAR, tab VARCHAR, lat DOUBLE, lon DOUBLE, last_checked DATE,
    first_seen_run VARCHAR, last_changed_run VARCHAR, fingerprint VARCHAR
);
CREATE TABLE IF NOT EXISTS company_member (company_id VARCHAR, candidate_id VARCHAR PRIMARY KEY);
-- geo: Nominatim results, cached forever (rule 4)
CREATE TABLE IF NOT EXISTS geocode (query VARCHAR PRIMARY KEY, lat DOUBLE, lon DOUBLE, display VARCHAR, fetched_at TIMESTAMP);
-- Task 2: KPIs per confirmed company, and the evidence for every kept value
CREATE TABLE IF NOT EXISTS kpi (
    company_id VARCHAR PRIMARY KEY, units_managed INTEGER, units_managed_text VARCHAR, segments VARCHAR,
    is_housing_company VARCHAR, metering_provider VARCHAR, remote_reading_mentioned VARCHAR, digital_tools VARCHAR,
    decision_maker VARCHAR, decision_maker_role VARCHAR, personal_email VARCHAR, email_basis VARCHAR,
    fields_with_evidence INTEGER, run_id VARCHAR
);
CREATE TABLE IF NOT EXISTS priority (company_id VARCHAR PRIMARY KEY, priority VARCHAR, why VARCHAR);
CREATE TABLE IF NOT EXISTS linkedin (
    company_id VARCHAR PRIMARY KEY, person VARCHAR, query VARCHAR, url VARCHAR, title VARCHAR, run_id VARCHAR
);
CREATE TABLE IF NOT EXISTS kpi_evidence (company_id VARCHAR, field VARCHAR, value VARCHAR, quote VARCHAR, url VARCHAR);
-- Review queue: one row per reason a stage found (classify/investigate read it).
CREATE TABLE IF NOT EXISTS review (candidate_id VARCHAR, stage VARCHAR, reason VARCHAR);
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
