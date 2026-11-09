"""Stage 4: Impressum -> legal data. The LLM is the primary extractor, regex the cross-check.

Per field: a value counts only if its quote appears verbatim in the page text (whitespace-
normalised) and contains the value. Merge: both valid and equal -> llm+regex; only one valid ->
that one; both valid but different -> the LLM's value, and the candidate goes to `review` with
"regex disagrees on <field>". Values go to `fact` for every candidate on the site's domain.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from pipeline.budget import Budget, CapExceeded
from pipeline.contract import Ctx, StageResult
from pipeline.llm import LLM, load_prompt
from pipeline.normalize import clean_ws, fold, legal_form, parse_postcode_city, parse_register, phone_e164

STAGE = "extract"
FIELDS = ["company_name", "legal_form", "street", "postcode", "city", "phone", "register_court", "register_number"]
REGEX_FIELDS = ["legal_form", "postcode", "city", "phone", "register_court", "register_number"]

_FIELD = {"type": "object", "properties": {"value": {"type": ["string", "null"]}, "quote": {"type": ["string", "null"]}},
          "required": ["value", "quote"], "additionalProperties": False}
SCHEMA = {"type": "object", "properties": {f: _FIELD for f in FIELDS}, "required": FIELDS, "additionalProperties": False}

_INVISIBLE = re.compile(r"[­​‌‍﻿]")


def norm(s: str | None) -> str:
    return clean_ws(_INVISIBLE.sub("", s or ""))


def evidence_ok(value: str | None, quote: str | None, text_norm: str) -> bool:
    if not value or not quote:
        return False
    q, v = norm(quote), norm(value)
    return bool(v) and q in text_norm and v in q


# ---------- regex cross-check ----------
_PHONE = re.compile(r"\b(?:tel(?:efon)?|fon|phone|mobil|telephone)\b\.?\s*[:.]?\s*(\+?\(?\d[\d\s/()\-–.]{5,}\d)", re.I)
_PLZ_LINE = re.compile(r"\b\d{5}\s+[A-ZÄÖÜ]")


def regex_extract(text: str) -> dict[str, tuple[str, str]]:
    """field -> (value, quote). Quotes are the matching lines, so evidence holds by construction."""
    out: dict[str, tuple[str, str]] = {}
    lines = [norm(x) for x in text.splitlines() if norm(x)]
    for ln in lines:
        if _PLZ_LINE.search(ln):
            pc, city = parse_postcode_city(ln)
            if pc:
                out["postcode"] = (pc, ln)
                if city:
                    out["city"] = (city, ln)
                break
    for ln in lines:
        if re.search(r"\bfax\b", ln, re.I) and not re.search(r"\btel", ln, re.I):
            continue
        m = _PHONE.search(ln)
        if m and phone_e164(m.group(1)):
            out["phone"] = (m.group(1).strip(), ln)
            break
    for ln in lines:
        reg = parse_register(ln)
        if reg:
            out["register_number"] = (reg.display().split(f"Amtsgericht {reg.court} ")[-1], ln)
            if reg.court:
                out["register_court"] = (reg.court, ln)
            break
    if "register_court" not in out and "register_number" in out:  # court on the line before/after
        joined = " ".join(lines)
        reg = parse_register(joined)
        if reg and reg.court:
            i = joined.find(reg.court)
            out["register_court"] = (reg.court, joined[max(0, i - 40): i + len(reg.court) + 10])
    for ln in lines[:20]:
        lf = legal_form(ln)
        if lf:
            out["legal_form"] = (lf, ln)
            break
    return out


# ---------- comparison ----------
def _court(s: str) -> str:
    return re.sub(r"\b(amtsgericht|registergericht|ag|berlin|in|beim)\b|[^a-z]", "", fold(s))


def canonical(field: str, value: str) -> str | None:
    """Normalised form used for comparison and storage."""
    if field == "phone":
        return phone_e164(value)
    if field == "register_number":
        reg = parse_register(value)
        return f"{reg.kind} {reg.number}{(' ' + reg.suffix) if reg.suffix else ''}" if reg else None
    if field == "postcode":
        m = re.search(r"\b\d{5}\b", value)
        return m.group(0) if m else None
    if field == "legal_form":
        return legal_form(value)
    return norm(value) or None


def same(field: str, a: str, b: str) -> bool:
    if field == "register_court":
        return _court(a) == _court(b)
    if field == "city":
        fa, fb = fold(a), fold(b)
        return fa == fb or fa.startswith(fb) or fb.startswith(fa)
    return (canonical(field, a) or "") == (canonical(field, b) or "")


def _nullish(s: str | None) -> str | None:
    """The model sometimes writes the string "null" instead of JSON null."""
    return None if s is None or s.strip().lower() in ("", "null", "none", "n/a", "-") else s


@dataclass
class FieldResult:
    value: str
    quote: str
    method: str


def merge(llm_out: dict, rx: dict[str, tuple[str, str]], text_norm: str) -> tuple[dict[str, FieldResult], list[str], dict]:
    """-> kept fields, review reasons, per-field stats."""
    kept: dict[str, FieldResult] = {}
    reasons: list[str] = []
    stats: dict[str, str] = {}
    for f in FIELDS:
        lv = _nullish((llm_out.get(f) or {}).get("value"))
        lq = _nullish((llm_out.get(f) or {}).get("quote"))
        l_ok = evidence_ok(lv, lq, text_norm)
        if lv and not l_ok:
            stats[f + ".llm_invalid"] = "1"
        r = rx.get(f)
        if l_ok and f in ("phone", "register_number", "postcode", "legal_form") and canonical(f, lv) is None:
            l_ok = False   # e.g. a phone number that does not parse
            stats[f + ".llm_invalid"] = "1"
        if l_ok and r:
            stats[f] = "agree" if same(f, lv, r[0]) else "disagree"
            if stats[f] == "agree":
                kept[f] = FieldResult(canonical(f, lv) or norm(lv), norm(lq), "llm+regex")
            else:
                kept[f] = FieldResult(canonical(f, lv) or norm(lv), norm(lq), "llm")
                reasons.append(f"regex disagrees on {f}")
        elif l_ok:
            kept[f] = FieldResult(canonical(f, lv) or norm(lv), norm(lq), "llm")
        elif r:
            kept[f] = FieldResult(canonical(f, r[0]) or r[0], r[1], "regex")
    return kept, reasons, stats


# ---------- stage ----------
def impressum_pages(ctx: Ctx) -> list[tuple[str, str, str]]:
    """(domain, url, text) per site with an Impressum (own page, or the home page on one-page sites)."""
    return ctx.con.execute(
        """SELECT s.domain, p.url, p.text FROM crawl_site s
           JOIN page p ON p.domain = s.domain
            AND p.page_type = CASE WHEN s.impressum_found_by = 'home' THEN 'home' ELSE 'impressum' END
           WHERE s.status = 'ok' AND s.impressum_url IS NOT NULL AND p.text IS NOT NULL
           ORDER BY s.domain"""
    ).fetchall()


def run_extract(ctx: Ctx, params: dict[str, Any] | None = None) -> StageResult:
    params = params or {}
    res = StageResult(run_id=ctx.run_id, stage=STAGE)
    pages = impressum_pages(ctx)
    cands_by_domain: dict[str, list[str]] = {}
    for cid, dom in ctx.con.execute("SELECT candidate_id, domain FROM website WHERE domain IS NOT NULL").fetchall():
        cands_by_domain.setdefault(dom, []).append(cid)
    if ctx.mode == "incremental":
        done = {r[0] for r in ctx.con.execute("SELECT DISTINCT candidate_id FROM fact WHERE method IN ('llm', 'regex', 'llm+regex')").fetchall()}
        pages = [p for p in pages if any(c not in done for c in cands_by_domain.get(p[0], []))]
    if params.get("limit"):
        pages = pages[: int(params["limit"])]

    caps = ctx.cfg["caps"]
    budget = Budget(ctx.con, "llm_extract", caps["llm_extract"], caps["llm_month"])
    try:
        llm = LLM(ctx.cfg["models"]["extract"], budget, ctx.cfg["llm"]["temperature"])
    except RuntimeError as e:
        res.errors.append(str(e))
        return res.finish("failed")
    system = load_prompt("extract", ctx.cfg["prompts"]["extract"])
    max_chars = ctx.cfg["llm"]["max_text_chars"]
    users = [f"Website: {url}\n\nImpressum text:\n<<<\n{text[:max_chars]}\n>>>" for _, url, text in pages]
    print(f"[extract] {len(pages)} Impressum pages, model {llm.model}")
    try:
        outs = llm.run_many(system, users, "impressum", SCHEMA, ctx.cfg["llm"]["concurrency"])
    except CapExceeded as e:
        res.errors.append(f"cap reached: {e}")
        return res.finish("failed")

    filled: Counter[str] = Counter()
    llm_invalid: Counter[str] = Counter()
    agree: Counter[str] = Counter()
    both: Counter[str] = Counter()
    review_reasons: Counter[str] = Counter()
    failed_calls = 0
    fact_rows: list[list] = []
    review_rows: list[list] = []
    touched: list[str] = []
    for (domain, url, text), out in zip(pages, outs):
        cids = cands_by_domain.get(domain, [])
        touched += cids
        if isinstance(out, Exception):
            failed_calls += 1
            res.errors.append(f"{domain}: {type(out).__name__}: {str(out)[:150]}")
            out = {}
        kept, reasons, stats = merge(out, regex_extract(text), norm(text))
        for f in kept:
            filled[f] += 1
        for k, v in stats.items():
            if k.endswith(".llm_invalid"):
                llm_invalid[k.split(".")[0]] += 1
            else:
                both[k] += 1
                agree[k] += v == "agree"
        for r in reasons:
            review_reasons[r] += 1
        for cid in cids:
            fact_rows += [[cid, f, fr.value, fr.quote, url, fr.method] for f, fr in kept.items()]
            review_rows += [[cid, STAGE, r] for r in reasons]

    if touched:   # idempotent: replace this stage's facts and review reasons for these candidates
        ctx.con.execute("CREATE TEMP TABLE IF NOT EXISTS _touched (candidate_id VARCHAR)")
        ctx.con.execute("DELETE FROM _touched")
        ctx.con.executemany("INSERT INTO _touched VALUES (?)", [[c] for c in set(touched)])
        ctx.con.execute("DELETE FROM fact WHERE method IN ('llm', 'regex', 'llm+regex') AND candidate_id IN (SELECT candidate_id FROM _touched)")
        ctx.con.execute("DELETE FROM review WHERE stage = ? AND candidate_id IN (SELECT candidate_id FROM _touched)", [STAGE])
    if fact_rows:
        ctx.con.executemany("INSERT INTO fact VALUES (?, ?, ?, ?, ?, ?)", fact_rows)
    if review_rows:
        ctx.con.executemany("INSERT INTO review VALUES (?, ?, ?)", review_rows)

    res.counts = {
        "impressum_pages": len(pages),
        "candidates_covered": len(set(touched)),
        "fields_filled": {f: filled[f] for f in FIELDS},
        "llm_calls": llm.calls,
        "llm_cache_hits": llm.cache_hits,
        "llm_failed_calls": failed_calls,
        "llm_values_rejected_by_evidence_check": {f: llm_invalid[f] for f in FIELDS if llm_invalid[f]},
        "llm_regex_agreement": {f: f"{agree[f]}/{both[f]} ({100 * agree[f] // both[f]}%)" for f in REGEX_FIELDS if both[f]},
        "review_reasons": dict(review_reasons),
        "candidates_to_review": len({r[0] for r in review_rows}),
        "tokens_in": llm.tokens_in,
        "tokens_out": llm.tokens_out,
    }
    res.api_calls["llm_extract"] = budget.used
    for k, v in res.counts.items():
        ctx.log(STAGE, k, v)
    if failed_calls > len(pages) // 10:   # many failed calls = a real problem, not noise
        return res.finish("failed")
    res.errors = res.errors[:20]
    return res.finish("ok")
