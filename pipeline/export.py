"""Stage 8: write the Google Sheet and remove all transient Google data.

Tabs: Companies, Excluded, Not in Berlin, Sources, Run log. Per tab: clear, one update, freeze the
header, basic filter, auto-size. Afterwards (CLAUDE.md rule 1):
- website URLs that came from Google Maps are kept only if our own crawl reached that site;
- resolve details that contain a Google name are cleared;
- serper_maps_tmp and places_tmp are dropped, and the stage checks they are gone.
"""
from __future__ import annotations

from itertools import combinations
from typing import Any

from pipeline.config import ROOT, env
from pipeline.contract import Ctx, StageResult
from pipeline.db import GOOGLE_TMP_TABLES, drop_google_tmp

STAGE = "export"
COMPANY_COLS = ["company_id", "company_name", "legal_form", "street", "postcode", "bezirk", "phone", "website",
                "impressum_url", "register_number", "sources", "source_count", "google_place_id", "status",
                "review_reason", "decided_by", "last_checked"]
ATTRIBUTION = [
    ["© OpenStreetMap contributors (ODbL), https://www.openstreetmap.org/copyright"],
    ["Bezirk boundaries: Geoportal Berlin, Datenlizenz Deutschland – Namensnennung – Version 2.0 (dl-de/by-2-0)"],
    ["Geocoding: Nominatim / OpenStreetMap"],
]


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def tables(ctx: Ctx) -> dict[str, list[list[Any]]]:
    con = ctx.con
    companies = con.execute(
        f"SELECT {', '.join(COMPANY_COLS)} FROM company WHERE tab = 'companies' ORDER BY bezirk NULLS LAST, company_name"
    ).fetchall()
    excluded = con.execute(
        "SELECT company_name, website, sources, excluded_reason FROM company WHERE tab = 'excluded' ORDER BY excluded_reason, company_name"
    ).fetchall()
    not_berlin = con.execute(
        f"SELECT {', '.join(COMPANY_COLS)}, city FROM company WHERE tab = 'not_in_berlin' ORDER BY postcode, company_name"
    ).fetchall()

    # Sources: candidates found, companies contributed alone, overlap with each other source
    src_cands = dict(con.execute("SELECT source, count(*) FROM candidate GROUP BY 1").fetchall())
    comp_sources = [set(s.split(";")) for (s,) in con.execute(
        "SELECT sources FROM company WHERE tab IN ('companies', 'not_in_berlin')").fetchall()]
    names = sorted(src_cands)
    sources = [["source", "candidates_found", "companies_with_source", "unique_companies_contributed"]
               + [f"overlap_with_{o}" for o in names]]
    for s in names:
        sources.append([s, src_cands[s], sum(s in c for c in comp_sources), sum(c == {s} for c in comp_sources)]
                       + [("" if o == s else sum(s in c and o in c for c in comp_sources)) for o in names])
    sources += [[]] + [["Directory overlap tests / decisions (run_log, stage=decision)"]]
    sources += [[k, v] for k, v in con.execute(
        "SELECT key, value FROM run_log WHERE stage = 'decision' ORDER BY ts").fetchall()]
    sources += [[]] + ATTRIBUTION

    runlog = con.execute(
        """SELECT run_id, stage, key, value, ts FROM run_log
           WHERE stage NOT IN ('run') AND key NOT LIKE '%.skip' AND key <> 'http.skip'
           ORDER BY ts"""
    ).fetchall()
    return {
        "Companies": [COMPANY_COLS] + [list(r) for r in companies],
        "Excluded": [["name", "website", "sources", "reason"]] + [list(r) for r in excluded],
        "Not in Berlin": [COMPANY_COLS + ["city"]] + [list(r) for r in not_berlin],
        "Sources": sources,
        "Run log": [["run_id", "stage", "key", "value", "timestamp"]] + [list(r) for r in runlog],
    }


def write_sheet(data: dict[str, list[list[Any]]]) -> None:
    import gspread
    sa = env("GOOGLE_SA_JSON") or "./sa.json"
    gc = gspread.service_account(filename=str((ROOT / sa).resolve()) if not sa.startswith("/") else sa)
    sh = gc.open_by_key(env("SHEET_ID"))
    existing = {ws.title: ws for ws in sh.worksheets()}
    for title, rows in data.items():
        rows = [[_cell(v) for v in r] for r in rows]
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        ws = existing.get(title) or sh.add_worksheet(title=title, rows=len(rows) + 10, cols=width)
        ws.clear()
        ws.resize(rows=max(len(rows), 2), cols=width)
        ws.update(range_name="A1", values=rows, value_input_option="RAW")
        ws.freeze(rows=1)
        if title not in ("Sources",):
            ws.set_basic_filter()
        ws.columns_auto_resize(0, width - 1)
    # the default empty sheet of a new spreadsheet
    if "Sheet1" in existing and "Sheet1" not in data and len(sh.worksheets()) > 1:
        sh.del_worksheet(existing["Sheet1"])


def scrub_google(ctx: Ctx) -> dict[str, int]:
    """Remove Google-derived values before the tmp tables go (rule 1)."""
    con = ctx.con
    out: dict[str, int] = {}
    has_tmp = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'serper_maps_tmp'").fetchone()[0]
    if has_tmp:
        # resolve search queries built from a Google name
        out["resolve_details_cleared"] = con.execute(
            """UPDATE website SET detail = NULL WHERE method IN ('search', 'none') AND detail IS NOT NULL
               AND EXISTS (SELECT 1 FROM serper_maps_tmp t WHERE t.name IS NOT NULL AND position(t.name IN website.detail) > 0)"""
        ).fetchone()[0]
    # Google website URLs: keep only where our own crawl reached the site
    out["google_websites_removed"] = con.execute(
        """UPDATE website SET url = NULL, domain = NULL, detail = 'removed: Google URL, site not reached by crawl'
           WHERE from_google AND url IS NOT NULL
             AND domain NOT IN (SELECT domain FROM crawl_site WHERE status = 'ok')"""
    ).fetchone()[0]
    drop_google_tmp(con)
    left = con.execute(
        f"SELECT count(*) FROM information_schema.tables WHERE table_name IN ({', '.join('?' * len(GOOGLE_TMP_TABLES))})",
        list(GOOGLE_TMP_TABLES)).fetchone()[0]
    out["google_tmp_tables_left"] = left
    return out


def run_export(ctx: Ctx, params: dict[str, Any] | None = None) -> StageResult:
    params = params or {}
    res = StageResult(run_id=ctx.run_id, stage=STAGE)
    if not env("SHEET_ID"):
        res.errors.append("SHEET_ID missing in .env")
        return res.finish("failed")
    ctx.log(STAGE, "started", "writing sheet")
    data = tables(ctx)
    res.counts = {f"rows.{t}": len(rows) - 1 for t, rows in data.items()}
    try:
        write_sheet(data)
    except Exception as e:   # sheet errors are reported; the Google cleanup below still runs
        res.errors.append(f"sheet: {type(e).__name__}: {str(e)[:300]}")
    if not params.get("keep_google_tmp"):
        res.counts |= scrub_google(ctx)
        if res.counts["google_tmp_tables_left"]:
            res.errors.append("Google tmp tables still present after drop")
    for k, v in res.counts.items():
        ctx.log(STAGE, k, v)
    return res.finish("failed" if res.errors else "ok")
