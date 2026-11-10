"""Stage 8: write the Google Sheet and remove all transient Google data.

Tabs, in this order: Leads, Details, KPI definitions, Priority rules, Evidence, Sources, Excluded,
Not in Berlin, Run log. Leads and Details hold every company on the companies tab (confirmed and
review). Per tab: clear, one update, freeze the header, filter, auto-size; Leads also freezes the
first column, wraps Why and colours Priority. Afterwards (CLAUDE.md rule 1):
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
    """All tabs, in sheet order."""
    con = ctx.con
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
    leads, details = leads_tables(con)
    evidence = con.execute(
        """SELECT co.company_name, e.company_id, e.field, e.value, e.quote, e.url
           FROM kpi_evidence e JOIN company co USING (company_id) ORDER BY co.company_name, e.field"""
    ).fetchall()
    return {
        "Leads": leads,
        "Details": details,
        "KPI definitions": kpi_definitions(con),
        "Priority rules": priority_rules(ctx.cfg["priority"]),
        "Evidence": [["Company", "ID", "Field", "Value", "Quote", "URL"]] + [list(r) for r in evidence],
        "Sources": sources,
        "Excluded": [["name", "website", "sources", "reason"]] + [list(r) for r in excluded],
        "Not in Berlin": [COMPANY_COLS + ["city"]] + [list(r) for r in not_berlin],
        "Run log": [["run_id", "stage", "key", "value", "timestamp"]] + [list(r) for r in runlog],
    }


# ---------- Leads and Details ----------
LEADS_HEADER = ["Company", "Priority", "Why", "Bezirk", "Website", "Phone", "Units managed", "Segments",
                "Contact person", "Email", "LinkedIn"]
DETAILS_HEADER = ["Company", "ID", "Address", "Status", "Review reason", "Sources", "Email source", "Legal form",
                  "Register number", "Impressum URL", "Google place ID", "Decided by", "Units basis", "Last checked"]
NO_WEBSITE = "Not checked – no website"
IN_REVIEW = "Not checked – in review"
PRIORITY_ORDER = {"High": 0, "Medium": 1, "Low": 2, IN_REVIEW: 3, NO_WEBSITE: 4}
PRIORITY_COLOURS = {"High": (0.80, 0.92, 0.80), "Medium": (1.00, 0.95, 0.75), "Low": (0.96, 0.84, 0.84)}
NOT_CHECKED_COLOUR = (0.90, 0.90, 0.90)


def priority_label(priority: str | None, website: str | None) -> str:
    """Confirmed companies have a priority; the rest were not checked for KPIs."""
    if priority:
        return priority
    return IN_REVIEW if website else NO_WEBSITE


def why_text(why: str | None, review_reason: str | None, provider: str | None, remote: str | None,
             tools: str | None) -> str:
    """The priority sentence, plus metering provider / remote reading / digital tools when found."""
    if not why:
        return review_reason or ""
    extra = []
    if provider and provider != "not named" and provider not in why:
        extra.append(f"metering provider {provider}")
    if remote == "yes" and "remote reading mentioned" not in why.replace("no remote reading mentioned", ""):
        extra.append("remote reading mentioned")
    if tools == "yes":
        extra.append("uses digital tools (portal/software)")
    return why + ("; " + ", ".join(extra) if extra else "")


def contact(name: str | None, role: str | None) -> str:
    return f"{name} ({role})" if name and role else name or ""


def leads_tables(con: Any) -> tuple[list[list[Any]], list[list[Any]]]:
    rows = con.execute(
        """SELECT co.company_name, p.priority, p.why, co.review_reason, k.metering_provider, k.remote_reading_mentioned,
                  k.digital_tools, co.bezirk, co.website, co.phone, k.units_managed, k.segments, k.decision_maker,
                  k.decision_maker_role, k.personal_email, l.url,
                  co.company_id, co.street, co.postcode, co.city, co.status, co.sources, k.email_basis, co.legal_form,
                  co.register_number, co.impressum_url, co.google_place_id, co.decided_by, k.units_managed_text,
                  co.last_checked
           FROM company co LEFT JOIN kpi k USING (company_id) LEFT JOIN priority p USING (company_id)
           LEFT JOIN linkedin l USING (company_id)
           WHERE co.tab = 'companies'"""
    ).fetchall()
    leads, details = [], []
    for (name, prio, why, reason, provider, remote, tools, bezirk, web, phone, units, segs, person, role, email,
         li, cid, street, pc, city, status, sources, email_basis, legal, reg, imp, place, by, units_text, checked) in rows:
        label = priority_label(prio, web)
        address = ", ".join(x for x in (street, " ".join(y for y in (pc, city) if y)) if x)
        leads.append([name, label, why_text(why, reason, provider, remote, tools), bezirk, web, phone, units, segs,
                      contact(person, role), email, li])
        details.append([name, cid, address, status, reason, sources, email_basis, legal, reg, imp, place, by,
                        units_text, checked])
    order = sorted(range(len(leads)), key=lambda i: (PRIORITY_ORDER[leads[i][1]], -(leads[i][6] or -1),
                                                     (leads[i][0] or "").lower()))
    return ([LEADS_HEADER] + [leads[i] for i in order],
            [DETAILS_HEADER] + sorted(details, key=lambda r: (r[0] or "").lower()))


# ---------- KPI definitions and priority rules ----------
IN_WHY = ["metering_provider", "remote_reading_mentioned", "digital_tools"]


def kpi_definitions(con: Any) -> list[list[Any]]:
    n = con.execute("SELECT count(*) FROM kpi").fetchone()[0] or 1
    found = dict(zip(IN_WHY, con.execute(
        """SELECT count(*) FILTER (metering_provider IS NOT NULL AND metering_provider <> 'not named'),
                  count(*) FILTER (remote_reading_mentioned = 'yes'), count(*) FILTER (digital_tools = 'yes')
           FROM kpi""").fetchone()))
    rate = {k: f"found for {v} of {n} confirmed companies ({v / n:.0%})" for k, v in found.items()}
    return [
        ["Leads column", "values", "why it matters to Heidi"],
        ["Priority", "High / Medium / Low / Not checked – in review / Not checked – no website",
         "Rules in the Priority rules tab. Not checked = no KPIs extracted (company not confirmed, or no website)"],
        ["Why", "sentence", "The rule that gave the priority, plus the KPIs below that live inside it"],
        ["Units managed", "number or empty", "More units = more meters = more revenue (stated wording in Details > Units basis)"],
        ["Segments", "WEG, Miet, SE, Gewerbe", "WEG and rental buildings need heating-cost billing"],
        ["Contact person", "name (role) from the Impressum or team page", "Who to contact"],
        ["Email", "personal address or empty", "Only a personal one (not info@, kontakt@, office@ ...); Details > Email source: "
                                               "published = on their site, pattern = derived from a colleague's address, MX checked"],
        ["LinkedIn", "URL or empty", "From a Serper search; kept only if the result shows the person's and the company's name"],
        [],
        ["KPIs inside \"Why\" (no own column: found too rarely to fill a column)"],
        ["metering_provider", "\"metering provider <name>\"", f"Named provider = locked in; none = open. {rate['metering_provider']}"],
        ["remote_reading_mentioned", "\"remote reading mentioned\"",
         f"Not mentioned = likely not ready for the 31.12.2026 retrofit deadline. {rate['remote_reading_mentioned']}"],
        ["digital_tools", "\"uses digital tools (portal/software)\"",
         f"Tenant portal or property software named = digitally mature. {rate['digital_tools']}"],
        ["High-priority Why texts also say \"no remote reading mentioned\" / \"no metering provider named\" when that made them open to switching."],
        [],
        ["Dropped: is_housing_company. Task 1 puts housing companies in review, so it was never \"yes\" for a confirmed company."],
        ["Empty = not found on the company's crawled pages; values are never guessed. Every value has a verbatim quote (Evidence tab)."],
    ]


def priority_rules(pc: dict[str, Any]) -> list[list[Any]]:
    return [
        ["priority", "rule (checked top to bottom; the first match wins)"],
        ["Low", f"No WEG or Miet segment found, or units_managed stated below {pc['low_units_below']}"],
        ["High", f"WEG or Miet, units_managed >= {pc['high_units_min']}, and open to switching "
                 "(remote reading not mentioned or no metering provider named)"],
        ["Medium", "Everything else (incl. WEG/Miet with unknown units)"],
        [],
        ["Thresholds live in config/berlin.yaml (priority.low_units_below, priority.high_units_min)."],
        [f"{IN_REVIEW}: company in review (reason in Why), no KPIs extracted. {NO_WEBSITE}: no website found."],
    ]


# ---------- writing ----------
TEXT_TABS = ("Sources", "KPI definitions", "Priority rules")
OBSOLETE_TABS = ("Companies", "Sheet1")   # replaced by Leads + Details / default sheet of a new spreadsheet
WHY_WIDTH_PX = 460


def _rgb(c: tuple[float, float, float]) -> dict[str, float]:
    return {"red": c[0], "green": c[1], "blue": c[2]}


def leads_format_requests(sheet_id: int, n_rows: int, old_rules: int) -> list[dict]:
    """Wrap Why (column C) at a fixed width; colour Priority (column B) by value."""
    col = lambda c: {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": n_rows, "startColumnIndex": c,  # noqa: E731
                     "endColumnIndex": c + 1}
    reqs: list[dict] = [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}} for _ in range(old_rules)]
    reqs += [
        {"repeatCell": {"range": col(2), "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                        "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
                                       "properties": {"pixelSize": WHY_WIDTH_PX}, "fields": "pixelSize"}},
    ]
    rules = [("TEXT_EQ", p, c) for p, c in PRIORITY_COLOURS.items()] + [("TEXT_STARTS_WITH", "Not checked", NOT_CHECKED_COLOUR)]
    for i, (kind, value, colour) in enumerate(rules):
        reqs.append({"addConditionalFormatRule": {"index": i, "rule": {
            "ranges": [col(1)],
            "booleanRule": {"condition": {"type": kind, "values": [{"userEnteredValue": value}]},
                            "format": {"backgroundColor": _rgb(colour)}}}}})
    return reqs


def write_sheet(data: dict[str, list[list[Any]]]) -> None:
    import gspread
    sa = env("GOOGLE_SA_JSON") or "./sa.json"
    gc = gspread.service_account(filename=str((ROOT / sa).resolve()) if not sa.startswith("/") else sa)
    sh = gc.open_by_key(env("SHEET_ID"))
    existing = {ws.title: ws for ws in sh.worksheets()}
    written = []
    for title, rows in data.items():
        rows = [[_cell(v) for v in r] for r in rows]
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        ws = existing.get(title) or sh.add_worksheet(title=title, rows=len(rows) + 10, cols=width)
        ws.clear()
        ws.resize(rows=max(len(rows), 2), cols=width)
        ws.update(range_name="A1", values=rows, value_input_option="RAW")
        ws.freeze(rows=1, cols=1 if title == "Leads" else 0)
        if title not in TEXT_TABS:
            ws.set_basic_filter()
        ws.columns_auto_resize(0, width - 1)
        if title == "Leads":
            meta = sh.fetch_sheet_metadata({"fields": "sheets(properties.sheetId,conditionalFormats)"})
            old = next((len(s.get("conditionalFormats", [])) for s in meta["sheets"]
                        if s["properties"]["sheetId"] == ws.id), 0)
            sh.batch_update({"requests": leads_format_requests(ws.id, len(rows), old)})
        written.append(ws)
    for title in OBSOLETE_TABS:
        if title in existing and title not in data:
            sh.del_worksheet(existing[title])
    sh.reorder_worksheets(written)


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
