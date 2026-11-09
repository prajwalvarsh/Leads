"""Stage 5: is each candidate a property manager? LLM primary, keyword rules as cross-check.

- Crawled sites: the LLM reads the name plus home/about/services text (one call per site) and
  returns is_property_manager (yes/no/unclear), services, is_housing_company and a quote.
  A quote that is not verbatim in the text makes the answer "unclear".
- Rules: keep-words / drop-words (config) on name, directory category and page text.
  A keep-word wins over a drop-word; neither means the rules have no opinion.
- Decision: LLM and rules agree -> apply (llm+rules). Rules have no opinion -> apply the LLM (llm).
  They disagree, or the LLM is unclear -> review. No crawled site -> rules only (rules);
  no rule match either -> review.
- Housing companies / cooperatives are kept with reason "housing company".
Drops go to `excluded`, review reasons to `review`.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from pipeline.budget import Budget, CapExceeded
from pipeline.contract import Ctx, StageResult
from pipeline.db import now
from pipeline.extract import norm
from pipeline.llm import LLM, load_prompt
from pipeline.normalize import fold

STAGE = "classify"
SERVICES = ["WEG", "Miet", "SE", "Gewerbe", "other"]
SCHEMA = {
    "type": "object",
    "properties": {
        "is_property_manager": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "services": {"type": "array", "items": {"type": "string", "enum": SERVICES}},
        "is_housing_company": {"type": "boolean"},
        "quote": {"type": "string"},
    },
    "required": ["is_property_manager", "services", "is_housing_company", "quote"],
    "additionalProperties": False,
}


_DASHES = str.maketrans({"–": "-", "—": "-", "‑": "-", "„": '"', "“": '"', "”": '"', "‚": "'", "‘": "'", "’": "'"})


def loose(s: str | None) -> str:
    """Verbatim check that ignores whitespace, case and dash/quote-mark variants (not words or numbers)."""
    return norm(s).translate(_DASHES).lower()


def rule_verdict(texts: list[str | None], keep: list[str], drop: list[str]) -> tuple[str, str | None]:
    """-> ("keep" | "drop" | "none", matched word). Keep-words win over drop-words."""
    t = fold(" ".join(x for x in texts if x))
    for w in keep:
        if fold(w) in t:
            return "keep", w
    for w in drop:
        if fold(w) in t:
            return "drop", w
    return "none", None


@dataclass
class Decision:
    status: str          # keep | drop | review
    decided_by: str
    reason: str | None


def decide(llm: str | None, rules: str, housing: bool = False) -> Decision:
    """llm: yes | no | unclear | None (no crawled site). rules: keep | drop | none."""
    if llm is None:
        if rules == "keep":
            return Decision("keep", "rules", None)
        if rules == "drop":
            return Decision("drop", "rules", "drop-word in name/category (no website)")
        return Decision("review", "rules", "no website and no rule match")
    if llm == "unclear":
        return Decision("review", "llm", "unclear whether property manager")
    want = "keep" if llm == "yes" else "drop"
    if rules == "none":
        d = Decision(want, "llm", None)
    elif rules == want:
        d = Decision(want, "llm+rules", None)
    else:
        return Decision("review", "llm", f"llm says {llm}, rules say {rules}")
    if d.status == "drop":
        d.reason = "not a property manager (llm)"
    if d.status == "keep" and housing:
        d.reason = "housing company"
    return d


def run_classify(ctx: Ctx, params: dict[str, Any] | None = None) -> StageResult:
    params = params or {}
    res = StageResult(run_id=ctx.run_id, stage=STAGE)
    cc = ctx.cfg["classify"]
    keep, drop = cc["keep_words"], cc["drop_words"]
    per_page = cc["max_chars_per_page"]

    # candidates with their names (Serper names only transiently, from serper_maps_tmp)
    cands = ctx.con.execute(
        """SELECT c.candidate_id, coalesce(c.name, t.name), coalesce(c.raw_category, t.type), w.domain
           FROM candidate c
           LEFT JOIN serper_maps_tmp t ON c.source = 'serper_maps' AND t.ref = c.source_ref
           LEFT JOIN website w USING (candidate_id)"""
    ).fetchall()
    if ctx.mode == "incremental":
        done = {r[0] for r in ctx.con.execute("SELECT candidate_id FROM classification").fetchall()}
        cands = [c for c in cands if c[0] not in done]

    pages: dict[str, dict[str, tuple[str, str]]] = {}   # domain -> page_type -> (url, text)
    for dom, ptype, url, text in ctx.con.execute(
        "SELECT domain, page_type, url, text FROM page WHERE page_type IN ('home', 'about', 'services') AND text IS NOT NULL"
    ).fetchall():
        pages.setdefault(dom, {})[ptype] = (url, text)
    imp_name = dict(ctx.con.execute(
        """SELECT w.domain, any_value(f.value) FROM fact f JOIN website w USING (candidate_id)
           WHERE f.field = 'company_name' GROUP BY 1"""
    ).fetchall())

    domains = sorted({c[3] for c in cands if c[3] in pages})
    if params.get("limit"):
        domains = domains[: int(params["limit"])]
        cands = [c for c in cands if c[3] in domains]

    names_by_domain: dict[str, list[str]] = {}
    for _, name, _, dom in cands:
        if dom and name:
            names_by_domain.setdefault(dom, []).append(name)

    def site_text(dom: str) -> str:
        return "\n\n".join(f"[{k}]\n{pages[dom][k][1][:per_page]}" for k in ("home", "about", "services") if k in pages[dom])

    users = []
    for dom in domains:
        names = sorted(set(names_by_domain.get(dom, [])))[:5]
        users.append(f"Website: {dom}\nCompany name (Impressum): {imp_name.get(dom) or 'unknown'}\n"
                     f"Names in directories: {'; '.join(names) or 'none'}\n\nWebsite text:\n<<<\n{site_text(dom)}\n>>>")

    caps = ctx.cfg["caps"]
    budget = Budget(ctx.con, "llm_classify", caps["llm_classify"], caps["llm_month"])
    try:
        llm = LLM(ctx.cfg["models"]["classify"], budget, ctx.cfg["llm"]["temperature"])
        print(f"[classify] {len(cands)} candidates, {len(domains)} sites for the LLM, model {llm.model}")
        outs = llm.run_many(load_prompt("classify", ctx.cfg["prompts"]["classify"]), users, "classification",
                            SCHEMA, ctx.cfg["llm"]["concurrency"])
    except (RuntimeError, CapExceeded) as e:
        res.errors.append(str(e))
        return res.finish("failed")

    site: dict[str, dict] = {}
    failed_calls = no_quote = 0
    for dom, out in zip(domains, outs):
        if isinstance(out, Exception):
            failed_calls += 1
            res.errors.append(f"{dom}: {type(out).__name__}: {str(out)[:150]}")
            continue
        quote = norm(out.get("quote"))
        valid = bool(quote) and loose(quote) in loose(site_text(dom))
        if not valid:
            quote = ""
            if out["is_property_manager"] != "unclear":
                no_quote += 1
                out = out | {"is_property_manager": "unclear"}
        url = next((pages[dom][k][0] for k in ("about", "services", "home")
                    if k in pages[dom] and quote and loose(quote) in loose(pages[dom][k][1])),
                   pages[dom].get("home", ("", ""))[0])
        site[dom] = out | {"quote": quote or None, "url": url}

    ts = now()
    rows, excl, reviews = [], [], []
    stats: Counter[str] = Counter()
    agree = both = 0
    for cid, name, cat, dom in cands:
        s = site.get(dom) if dom else None
        texts = [name, cat] + ([site_text(dom)] if s else [])
        rv, word = rule_verdict(texts, keep, drop)
        verdict = s["is_property_manager"] if s else None
        housing = bool(s and s["is_housing_company"])
        d = decide(verdict, rv, housing)
        if s and verdict != "unclear" and rv != "none":
            both += 1
            agree += (verdict == "yes") == (rv == "keep")
        rows.append([cid, d.status, d.decided_by, verdict, ",".join(s["services"]) if s else None, housing,
                     s["quote"] if s else None, s["url"] if s else None, rv, word, d.reason, ctx.run_id, ts])
        stats[d.status] += 1
        stats[f"decided_by.{d.decided_by}"] += 1
        if d.status == "drop":
            excl.append([cid, d.reason])
        elif d.status == "review":
            reviews.append([cid, STAGE, d.reason])
            stats[f"review.{d.reason}"] += 1
        if d.reason == "housing company":
            stats["housing_company"] += 1

    ids = [[r[0]] for r in rows]
    if ids:   # idempotent: replace this stage's rows for these candidates
        ctx.con.execute("CREATE TEMP TABLE IF NOT EXISTS _cls (candidate_id VARCHAR)")
        ctx.con.execute("DELETE FROM _cls")
        ctx.con.executemany("INSERT INTO _cls VALUES (?)", ids)
        ctx.con.execute("DELETE FROM classification WHERE candidate_id IN (SELECT candidate_id FROM _cls)")
        ctx.con.execute("DELETE FROM review WHERE stage = ? AND candidate_id IN (SELECT candidate_id FROM _cls)", [STAGE])
        ctx.con.execute("DELETE FROM excluded WHERE candidate_id IN (SELECT candidate_id FROM _cls)")
        ctx.con.executemany("INSERT INTO classification VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    if excl:
        ctx.con.executemany("INSERT INTO excluded VALUES (?, ?)", excl)
    if reviews:
        ctx.con.executemany("INSERT INTO review VALUES (?, ?, ?)", reviews)

    llm_verdicts = Counter(s["is_property_manager"] for s in site.values())
    res.counts = {
        "candidates": len(cands),
        "kept": stats["keep"],
        "dropped": stats["drop"],
        "review": stats["review"],
        "decided_by": {k.split(".", 1)[1]: v for k, v in stats.items() if k.startswith("decided_by.")},
        "review_reasons": {k.split(".", 1)[1]: v for k, v in stats.items() if k.startswith("review.")},
        "housing_companies_kept": stats["housing_company"],
        "sites_classified_by_llm": len(site),
        "llm_verdicts_per_site": dict(llm_verdicts),
        "llm_answers_without_valid_quote": no_quote,
        "llm_rules_agreement": f"{agree}/{both} ({100 * agree // both if both else 0}%)",
        "llm_calls": llm.calls,
        "llm_cache_hits": llm.cache_hits,
        "llm_failed_calls": failed_calls,
        "tokens_in": llm.tokens_in,
        "tokens_out": llm.tokens_out,
    }
    res.api_calls["llm_classify"] = budget.used
    for k, v in res.counts.items():
        ctx.log(STAGE, k, v)
    res.errors = res.errors[:20]
    return res.finish("failed" if failed_calls > max(10, len(domains) // 10) else "ok")
