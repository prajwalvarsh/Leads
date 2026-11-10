"""Task 2, stage linkedin: one Serper search per decision maker, High priority first.

Query: "<name>" "<company>" site:linkedin.com/in  (academic titles removed from the name, legal form
removed from the company name). Keep the first linkedin.com/in/ URL whose result title or snippet
contains the person's name and the company name; otherwise empty. LinkedIn pages are never opened.
Stops when the remaining Serper credits drop below linkedin.stop_below_credits. A company that was
searched once is not searched again (no double spending on reruns).
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any

from pipeline.budget import Budget, CapExceeded
from pipeline.config import CACHE
from pipeline.contract import Ctx, StageResult
from pipeline.normalize import LEGAL_FORMS, fold
from pipeline.resolve import GENERIC
from pipeline.sources.serper_maps import SerperClient, credits_left, skip_reason

STAGE = "linkedin"
TITLES = re.compile(r"\b(Herr|Frau|Dr|Prof|Dipl|Ing|Kfm|Kffr|MBA|LL\.?M|B\.?A|M\.?A|M\.?Sc|B\.?Sc)\b\.?(-\w+\.?)?\s*", re.I)
PRIORITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}
SERPER_CACHE = CACHE / "serper"


def clean_person(name: str) -> str:
    return re.sub(r"\s+", " ", TITLES.sub(" ", name)).strip(" ,.-")


def clean_company(name: str) -> str:
    low = name
    for _, pat in LEGAL_FORMS:
        low = re.sub(pat, " ", low, flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"[\"“”„()]", " ", low)).strip(" ,.-–&")


def company_words(company: str, person: str) -> list[str]:
    """Distinctive words of the company name (not generic, not the person's own name)."""
    own = set(fold(person).replace("-", " ").split())
    return [w for w in re.findall(r"[a-z0-9]+", fold(company)) if len(w) >= 3 and w not in GENERIC and w not in own]


def mentions(title: str, snippet: str, person: str, company: str) -> bool:
    """The profile title starts with the person's name (LinkedIn: "Name – Role – Company"), and the
    title or snippet names the company: all its distinctive words, or the full name if it has none."""
    head = fold(re.split(r"\s[-–|]\s", title, maxsplit=1)[0])
    parts = [p for p in fold(person).replace("-", " ").split() if len(p) > 1]
    if len(parts) < 2 or not (parts[0] in head and parts[-1] in head):
        return False
    text = fold(f"{title} {snippet}")
    words = company_words(company, person)
    return all(w in text for w in words) if words else fold(company) in text


def pick(data: dict, person: str, company: str) -> tuple[str, str] | None:
    for r in data.get("organic") or []:
        link = r.get("link") or ""
        if "linkedin.com/in/" not in link:
            continue
        if mentions(r.get("title", ""), r.get("snippet", ""), person, company):
            return link.split("?")[0], f"{r.get('title', '')} | {r.get('snippet', '')}"[:500]
    return None


def _cache_path(q: str):  # noqa: ANN202
    return SERPER_CACHE / f"{hashlib.sha1(q.encode()).hexdigest()}.json"


def recheck(ctx: Ctx) -> int:
    """Apply the current match rule to stored results (title only for rows found before the cache existed)."""
    removed = 0
    for cid, person, company, url, title in ctx.con.execute(
        """SELECT l.company_id, l.person, co.company_name, l.url, l.title FROM linkedin l JOIN company co USING (company_id)
           WHERE l.url IS NOT NULL"""
    ).fetchall():
        t, _, s = (title or "").partition(" | ")
        if not mentions(t, s, clean_person(person), clean_company(company)):
            ctx.con.execute("UPDATE linkedin SET url = NULL WHERE company_id = ?", [cid])
            removed += 1
    return removed


def run_linkedin(ctx: Ctx, params: dict[str, Any] | None = None) -> StageResult:
    res = StageResult(run_id=ctx.run_id, stage=STAGE)
    SERPER_CACHE.mkdir(parents=True, exist_ok=True)
    rechecked_removed = recheck(ctx)
    rows = ctx.con.execute(
        """SELECT co.company_id, co.company_name, k.decision_maker, p.priority, coalesce(k.units_managed, 0)
           FROM kpi k JOIN company co USING (company_id) JOIN priority p USING (company_id)
           WHERE k.decision_maker IS NOT NULL AND co.status = 'confirmed (website)'
             AND co.company_id NOT IN (SELECT company_id FROM linkedin)"""
    ).fetchall()
    rows.sort(key=lambda r: (PRIORITY_ORDER.get(r[3], 9), -r[4], r[0]))

    total = ctx.cfg["caps"]["serper_total"]
    stop = ctx.cfg["linkedin"]["stop_below_credits"]
    skip = skip_reason(ctx.con, total, stop + 1)
    if skip:   # no key or no credits above the floor: skipped, not failed
        res.counts = {"skipped": skip, "decision_makers_not_searched": len(rows),
                      "earlier_matches_removed_by_recheck": rechecked_removed,
                      "linkedin_urls_total": ctx.con.execute("SELECT count(url) FROM linkedin").fetchone()[0]}
        for k, v in res.counts.items():
            ctx.log(STAGE, k, v)
        print(f"[linkedin] skipped ({skip}; stops below {stop})")
        return res.finish("skipped")
    used = total - credits_left(ctx.con, total)
    cap = max(0, total - used - stop)
    budget = Budget(ctx.con, "serper", cap, total, all_time=True)
    print(f"[linkedin] {len(rows)} decision makers to search, {total - used} Serper credits left, cap {cap}")
    searched, found, by_prio, not_searched = 0, 0, Counter(), 0
    client = SerperClient(budget) if rows and cap > 0 else None
    for i, (cid, company, person, prio, _) in enumerate(rows):
        if client is None or budget.remaining() < 1:
            not_searched = len(rows) - i
            break
        p, c = clean_person(person), clean_company(company)
        q = f'"{p}" "{c}" site:linkedin.com/in'
        path = _cache_path(q)
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
            else:
                data = client.search(q)
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except CapExceeded:
            not_searched = len(rows) - i
            break
        except RuntimeError as e:
            res.errors.append(str(e))
            not_searched = len(rows) - i
            break
        searched += 1
        hit = pick(data, p, c)
        if hit:
            found += 1
            by_prio[prio] += 1
        ctx.con.execute("INSERT OR REPLACE INTO linkedin VALUES (?, ?, ?, ?, ?, ?)",
                        [cid, person, q, hit[0] if hit else None, hit[1] if hit else None, ctx.run_id])
    res.counts = {
        "decision_makers_to_search": len(rows),
        "searches": searched,
        "linkedin_urls_found": found,
        "found_by_priority": dict(by_prio),
        "not_searched_credit_limit": not_searched,
        "serper_credits_left": total - used - budget.used,
        "earlier_matches_removed_by_recheck": rechecked_removed,
        "linkedin_urls_total": ctx.con.execute("SELECT count(url) FROM linkedin").fetchone()[0],
    }
    res.api_calls["serper_credits"] = budget.used
    for k, v in res.counts.items():
        ctx.log(STAGE, k, v)
    return res.finish("failed" if res.errors else "ok")
