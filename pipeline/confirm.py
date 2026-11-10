"""Stage confirm (after geo): final status of every row on the companies tab.

- confirmed (website): has a website, and every review reason is only a flag: "<field> uncertain"
  (was "regex disagrees on <field>"; the LLM value is kept) or one of confirm.website_flags
  (e.g. "housing company"). The flags stay in review_reason so Details shows them.
- confirmed (listing): no website, has a Bezirk, a non-Google directory lists it under a
  property-management category (confirm.listing_categories) and the name contains a
  property-management word (confirm.listing_name_words). decided_by = "listing"; the review
  reason (e.g. "no Impressum found") stays as a note.
- review: everything else (unclear whether a property manager, no Impressum, Berlin not confirmed ...).
- confirm.overrides: manual fixes checked by hand (name and/or status), applied last; decided_by = "manual".
  merge_into: <company_id> merges a duplicate into that company (members and sources move over).
Idempotent: reads the reasons dedupe/geo wrote (or this stage's own rewritten flags).
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from pipeline.contract import Ctx, StageResult
from pipeline.db import CONFIRMED_LISTING, CONFIRMED_WEBSITE, REVIEW

STAGE = "confirm"
DISAGREE = "regex disagrees on "
UNCERTAIN = " uncertain"


def flag_reasons(reason: str | None) -> list[str]:
    """Split a review reason; "regex disagrees on register_number" -> "register number uncertain"."""
    out = []
    for p in (reason or "").split(";"):
        p = p.strip()
        if p.startswith(DISAGREE):
            p = p.removeprefix(DISAGREE).replace("_", " ") + UNCERTAIN
        if p:
            out.append(p)
    return list(dict.fromkeys(out))


def website_status(parts: list[str], website_flags: list[str]) -> str:
    blocking = [p for p in parts if not p.endswith(UNCERTAIN) and p not in website_flags]
    return REVIEW if blocking else CONFIRMED_WEBSITE


def listed_as_manager(name: str | None, categories: list[tuple[str, str | None]], cfg: dict[str, Any]) -> bool:
    cats = cfg["listing_categories"]
    in_category = any(cat and any(cat.startswith(c) for c in cats.get(src, [])) for src, cat in categories)
    low = (name or "").lower()
    return in_category and any(w in low for w in cfg["listing_name_words"])


def merge_duplicates(ctx: Ctx, overrides: dict[str, dict]) -> int:
    """Overrides with merge_into: the duplicate's members and sources move to the target; the duplicate row goes."""
    merged = 0
    for dup, o in overrides.items():
        target = str(o.get("merge_into") or "")
        rows = dict(ctx.con.execute("SELECT company_id, sources FROM company WHERE company_id IN (?, ?)", [dup, target]).fetchall())
        if not target or dup not in rows or target not in rows:
            continue   # already merged (rerun) or ids gone
        sources = sorted({s for v in rows.values() for s in (v or "").split(";") if s})
        ctx.con.execute("UPDATE company SET sources = ?, source_count = ? WHERE company_id = ?",
                        [";".join(sources), len(sources), target])
        ctx.con.execute("UPDATE company_member SET company_id = ? WHERE company_id = ?", [target, dup])
        ctx.con.execute("DELETE FROM company WHERE company_id = ?", [dup])
        merged += 1
    return merged


def run_confirm(ctx: Ctx, params: dict[str, Any] | None = None) -> StageResult:
    res = StageResult(run_id=ctx.run_id, stage=STAGE)
    cfg = ctx.cfg["confirm"]
    overrides = {str(k): v for k, v in (cfg.get("overrides") or {}).items()}
    rows = ctx.con.execute(
        """SELECT co.company_id, co.company_name, co.website, co.bezirk, co.status, co.review_reason, co.decided_by,
                  list([ca.source, ca.raw_category]) FILTER (WHERE ca.source IS NOT NULL)
           FROM company co LEFT JOIN company_member m USING (company_id) LEFT JOIN candidate ca USING (candidate_id)
           WHERE co.tab = 'companies' GROUP BY ALL"""
    ).fetchall()
    before = Counter(r[4] for r in rows)
    updates = []
    for cid, name, web, bezirk, status, reason, by, cats in rows:
        if web:
            parts = flag_reasons(reason)
            new_status, new_reason, new_by = website_status(parts, cfg["website_flags"]), "; ".join(parts) or None, by
            if by == "listing":   # was a listing row; website found since
                new_by = None
        elif bezirk and listed_as_manager(name, [(s, c) for s, c in cats or []], cfg):
            # the review reason (e.g. "no Impressum found") stays as a note; decided_by says why it is confirmed
            new_status, new_reason, new_by = CONFIRMED_LISTING, reason, "listing"
        else:
            new_status, new_reason, new_by = REVIEW, reason, None if by == "listing" else by
        o = overrides.get(cid, {})
        if "status" in o:
            new_status, new_reason, new_by = o["status"], o.get("review_reason"), "manual"
        updates.append([new_status, new_reason, new_by, o.get("company_name", name), cid])
    ctx.con.executemany(
        "UPDATE company SET status = ?, review_reason = ?, decided_by = ?, company_name = ? WHERE company_id = ?", updates)
    merged = merge_duplicates(ctx, overrides)
    after = Counter(r[0] for r in ctx.con.execute("SELECT status FROM company WHERE tab = 'companies'").fetchall())
    res.counts = {"before": dict(before), "after": dict(after),
                  "newly_confirmed_website": sum(1 for u, r in zip(updates, rows)
                                                 if u[0] == CONFIRMED_WEBSITE and r[4] not in ("confirmed", CONFIRMED_WEBSITE)),
                  "newly_confirmed_listing": sum(1 for u, r in zip(updates, rows)
                                                 if u[0] == CONFIRMED_LISTING and r[4] != CONFIRMED_LISTING),
                  "merged_duplicates": merged}
    for k, v in res.counts.items():
        ctx.log(STAGE, k, v)
    return res.finish("ok")
