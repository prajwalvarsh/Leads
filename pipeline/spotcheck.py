"""Spot check (Task 1 definition of done), independent of the pipeline's extraction.

20 random `confirmed` companies (fixed seed). Each Impressum is fetched fresh (no cache; robots.txt
and rate limit still apply), turned into text with a plain selectolax body dump (not the pipeline's
trafilatura path), and read by `models.check` with its own prompt. The model never sees the sheet
values. Results are compared field by field and written to docs/spotcheck.md, mismatches first.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any

from rapidfuzz import fuzz
from selectolax.lexbor import LexborHTMLParser

from pipeline.budget import Budget
from pipeline.config import ROOT
from pipeline.contract import Ctx, StageResult
from pipeline.http import Fetcher, Response
from pipeline.extract import norm as norm_text
from pipeline.llm import LLM, load_prompt
from pipeline.normalize import clean_ws, fold, normalise_name, parse_register, phone_e164, split_street

STAGE = "spotcheck"
FIELDS = ["company_name", "street", "postcode", "phone", "register_number", "is_property_manager"]
_F = {"type": "object", "properties": {"value": {"type": ["string", "null"]}, "quote": {"type": ["string", "null"]}},
      "required": ["value", "quote"], "additionalProperties": False}
SCHEMA = {"type": "object", "properties": {f: _F for f in FIELDS}, "required": FIELDS, "additionalProperties": False}
DOC = ROOT / "docs" / "spotcheck.md"


def page_text(html: str) -> str:
    tree = LexborHTMLParser(html)
    for sel in ("script", "style", "noscript", "svg"):
        for n in tree.css(sel):
            n.decompose()
    raw = tree.body.text(separator="\n") if tree.body else ""
    return "\n".join(x for x in (clean_ws(l) for l in raw.splitlines()) if x)


def _null(v: str | None) -> str | None:
    return None if v is None or v.strip().lower() in ("", "null", "none") else v.strip()


def compare(field: str, sheet: str | None, check: str | None) -> bool:
    if field == "is_property_manager":
        return check == "yes"          # every sampled row is confirmed as a property manager
    sheet, check = (norm_text(sheet) or None), (norm_text(check) or None)
    if not sheet and not check:
        return True
    if not sheet or not check:
        return False
    if field == "phone":
        return phone_e164(sheet) == phone_e164(check)
    if field == "register_number":
        a, b = parse_register(sheet), parse_register(check)
        return bool(a and b and a.key == b.key)
    if field == "postcode":
        return re.sub(r"\D", "", sheet) == re.sub(r"\D", "", check)
    if field == "street":
        return split_street(sheet) == split_street(check)
    # company_name: same after folding, punctuation and whitespace (legal form included)
    norm = lambda s: re.sub(r"[^a-z0-9]", "", fold(s))  # noqa: E731
    return norm(sheet) == norm(check) or (normalise_name(sheet) == normalise_name(check)
                                          and fuzz.ratio(norm(sheet), norm(check)) >= 90)


async def _fetch(urls: list[str]) -> list[str | None]:
    async with Fetcher() as f:
        res = await asyncio.gather(*(f.fetch(u, use_cache=False) for u in urls))
    out: list[str | None] = []
    for r in res:
        out.append(page_text(r.text) if isinstance(r, Response) and r.status < 400 else None)
    return out


def run_spotcheck(ctx: Ctx, params: dict[str, Any] | None = None) -> StageResult:
    params = params or {}
    seed, n = int(params.get("seed", 20261109)), int(params.get("n", 20))
    res = StageResult(run_id=ctx.run_id, stage=STAGE)
    rows = ctx.con.execute(
        """SELECT company_id, company_name, street, postcode, phone, register_number, impressum_url, website, decided_by
           FROM company WHERE tab = 'companies' AND status = 'confirmed (website)' ORDER BY company_id"""
    ).fetchall()
    sample = random.Random(seed).sample(rows, min(n, len(rows)))
    texts = asyncio.run(_fetch([r[6] for r in sample]))

    model = ctx.cfg["models"]["check"]
    llm = LLM(model, Budget(ctx.con, "llm_check", ctx.cfg["caps"]["llm_check"], ctx.cfg["caps"]["llm_month"]), 0)
    system = load_prompt("check", 1)
    users = [f"Impressum text:\n<<<\n{t[:12000]}\n>>>" if t else "" for t in texts]
    idx = [i for i, u in enumerate(users) if u]
    outs = llm.run_many(system, [users[i] for i in idx], "check", SCHEMA, 5)
    by_row: dict[int, dict] = {i: o for i, o in zip(idx, outs)}

    table, mismatches = [], []
    correct = {f: 0 for f in FIELDS}
    checked = {f: 0 for f in FIELDS}
    for i, r in enumerate(sample):
        cid, name, street, pc, phone, reg, imp, web, by = r
        sheet = {"company_name": name, "street": street, "postcode": pc, "phone": phone,
                 "register_number": reg, "is_property_manager": "yes"}
        out = by_row.get(i)
        for f in FIELDS:
            if out is None or isinstance(out, Exception):
                cv, q, ok = None, ("fetch failed" if texts[i] is None else f"check failed: {out}"), None
            else:
                cv, q = _null(out[f]["value"]), _null(out[f]["quote"]) or ""
                ok = compare(f, sheet[f], cv)
                checked[f] += 1
                correct[f] += ok
            line = {"row": i + 1, "company_id": cid, "url": imp, "field": f, "sheet": sheet[f] or "",
                    "check": cv or "", "match": {True: "yes", False: "no", None: "n/a"}[ok], "quote": q}
            table.append(line)
            if ok is False:
                mismatches.append(line)

    accuracy = {f: f"{correct[f]}/{checked[f]}" + (f" ({100 * correct[f] // checked[f]}%)" if checked[f] else "")
                for f in FIELDS}
    raw = {"seed": seed, "model": model, "sample": [r[0] for r in sample], "mismatches": mismatches,
           "table": table, "accuracy": accuracy}
    (ROOT / "eval").mkdir(exist_ok=True)
    name = "spotcheck_results.json" if seed == 20261109 else f"spotcheck_results_{seed}.json"   # first check keeps its file
    (ROOT / "eval" / name).write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    res.counts = {"seed": seed, "rows": len(sample), "fetch_failed": sum(t is None for t in texts),
                  "mismatches": len(mismatches), "accuracy": accuracy, "llm_calls": llm.calls}
    res.api_calls["llm_check"] = llm.calls
    for k, v in res.counts.items():
        ctx.log(STAGE, k, v)
    return res.finish("ok")
