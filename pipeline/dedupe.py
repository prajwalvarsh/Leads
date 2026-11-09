"""Stage 6: dedupe candidates into companies (union-find).

Merge when ANY of: same register number; same registrable domain (not a shared host);
same E.164 phone; name token_set_ratio >= 90 AND same street + house number.
Never merge two clusters with different register numbers.
Primary key = register number, else domain, else phone, else name+street; company_id = sha1(key)[:12].
Company values: Impressum facts first, then directory/OSM values. Google (Serper) fields are used
only for matching, never written to `company`; a cluster with only Google data is excluded.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from rapidfuzz import fuzz

from pipeline.contract import Ctx, StageResult
from pipeline.normalize import is_listed_domain, legal_form, normalise_name, parse_register, phone_e164
from pipeline.overlap import street_key

STAGE = "dedupe"
NAME_SOURCES = ["gelbeseiten", "dasoertliche", "osm", "11880", "association", "search"]   # no serper_maps
PRIORITY = {"llm+rules": 3, "llm": 2, "rules": 1}


@dataclass
class Member:
    cid: str
    source: str
    name: str | None              # non-Google name (None for serper_maps)
    match_name: str               # normalised name for matching (may come from Google, transient)
    street: str | None            # non-Google street
    postcode: str | None
    city: str | None
    phone: str | None             # non-Google E.164
    match_phone: str | None       # incl. Google phone, transient
    match_street: tuple[str, str] | None
    domain: str | None
    reg: str | None               # register key from the Impressum
    facts: dict[str, tuple[str, str]] = field(default_factory=dict)   # field -> (value, source_url)
    status: str | None = None     # classification
    decided_by: str | None = None
    reason: str | None = None
    housing: bool = False
    reviews: list[str] = field(default_factory=list)


def load_members(ctx: Ctx) -> list[Member]:
    excluded = ctx.cfg["shared_hosts"] + ctx.cfg["portal_domains"]
    facts: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
    for cid, f, v, url in ctx.con.execute("SELECT candidate_id, field, value, source_url FROM fact").fetchall():
        facts[cid][f] = (v, url)
    reviews: dict[str, list[str]] = defaultdict(list)
    for cid, reason in ctx.con.execute("SELECT candidate_id, reason FROM review WHERE stage = 'extract'").fetchall():
        reviews[cid].append(reason)
    rows = ctx.con.execute(
        """SELECT c.candidate_id, c.source, c.name, c.street, c.postcode, c.city, c.phone,
                  t.name, t.address, t.phone, w.domain,
                  cl.status, cl.decided_by, cl.reason, cl.is_housing_company
           FROM candidate c
           LEFT JOIN serper_maps_tmp t ON c.source = 'serper_maps' AND t.ref = c.source_ref
           LEFT JOIN website w USING (candidate_id)
           LEFT JOIN classification cl USING (candidate_id)"""
    ).fetchall()
    out: list[Member] = []
    for (cid, src, name, street, pc, city, phone, t_name, t_addr, t_phone, dom,
         st, by, reason, housing) in rows:
        f = facts.get(cid, {})
        reg = parse_register(f["register_number"][0]) if "register_number" in f else None
        imp_name = f.get("company_name", (None,))[0]
        google_street = t_addr.split(",")[0] if t_addr else None
        out.append(Member(
            cid=cid, source=src, name=name,
            match_name=normalise_name(imp_name or name or t_name),
            street=street, postcode=pc, city=city, phone=phone_e164(phone),
            match_phone=phone_e164(f.get("phone", (None,))[0]) or phone_e164(phone) or phone_e164(t_phone),
            match_street=street_key(f.get("street", (None,))[0] or street or google_street),
            domain=dom if dom and not is_listed_domain(dom, excluded) else None,
            reg=reg.key if reg else None, facts=f, status=st, decided_by=by, reason=reason,
            housing=bool(housing), reviews=reviews.get(cid, []),
        ))
    return out


# ---------- clustering ----------
class UnionFind:
    def __init__(self, members: list[Member]) -> None:
        self.parent = list(range(len(members)))
        self.reg = [m.reg for m in members]
        self.blocked = 0

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.reg[ra] and self.reg[rb] and self.reg[ra] != self.reg[rb]:
            self.blocked += 1   # never merge different register numbers
            return False
        self.parent[ra] = rb
        self.reg[rb] = self.reg[rb] or self.reg[ra]
        return True


def cluster(members: list[Member], name_min: int, max_names_per_phone: int) -> tuple[list[list[Member]], dict]:
    uf = UnionFind(members)
    merges: Counter[str] = Counter()

    def buckets(key) -> dict[str, list[int]]:
        b: dict[str, list[int]] = defaultdict(list)
        for i, m in enumerate(members):
            k = key(m)
            if k:
                b[k].append(i)
        return b

    for reason, key in (("register", lambda m: m.reg), ("domain", lambda m: m.domain),
                        ("phone", lambda m: m.match_phone)):
        for ids in buckets(key).values():
            if reason == "phone" and len({members[i].match_name for i in ids}) > max_names_per_phone:
                merges["phone_skipped_shared"] += 1
                continue
            for j in ids[1:]:
                merges[reason] += uf.union(ids[0], j)
    by_street = buckets(lambda m: m.match_street[0] if m.match_street and m.match_street[1] and m.match_name else None)
    for ids in by_street.values():
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                a, b = members[ids[x]], members[ids[y]]
                if a.match_street == b.match_street and fuzz.token_set_ratio(a.match_name, b.match_name) >= name_min:
                    merges["name_street"] += uf.union(ids[x], ids[y])
    groups: dict[int, list[Member]] = defaultdict(list)
    for i, m in enumerate(members):
        groups[uf.find(i)].append(m)
    merges["blocked_different_register"] = uf.blocked
    return list(groups.values()), dict(merges)


# ---------- company row ----------
def primary_key(ms: list[Member]) -> str:
    def top(vals: list[str | None]) -> str | None:
        c = Counter(v for v in vals if v)
        return c.most_common(1)[0][0] if c else None
    reg = top([m.reg for m in ms])
    if reg:
        return f"reg:{reg}"
    dom = top([m.domain for m in ms])
    if dom:
        return f"domain:{dom}"
    phone = top([m.match_phone for m in ms])
    if phone:
        return f"phone:{phone}"
    m = ms[0]
    street = " ".join(m.match_street) if m.match_street else ""
    return f"name:{m.match_name}|{street}" if m.match_name else f"cid:{min(x.cid for x in ms)}"


def verdict(ms: list[Member]) -> tuple[str, str, list[str]]:
    """Classification of the cluster from its best-evidence members -> (status, decided_by, reasons)."""
    rated = [m for m in ms if m.status]
    if not rated:
        return "review", "rules", ["not classified"]
    best = max(PRIORITY.get(m.decided_by or "", 0) for m in rated)
    top = [m for m in rated if PRIORITY.get(m.decided_by or "", 0) == best]
    statuses = {m.status for m in top}
    by = "llm" if best >= 2 else "rules"   # sheet values: rules | llm | agent
    if statuses == {"drop"}:
        return "drop", by, [next(m.reason for m in top if m.reason)]
    if statuses == {"keep"}:
        return "keep", by, []
    reasons = sorted({m.reason for m in top if m.status == "review" and m.reason}) or ["members disagree"]
    return "review", by, reasons


def build_company(ms: list[Member], place_ids: dict[str, str], crawl: dict[str, tuple[str, str]]) -> dict[str, Any]:
    pk = primary_key(ms)
    with_facts = [m for m in ms if m.facts]
    # facts of the most common domain among members with an Impressum
    best = None
    if with_facts:
        dom = Counter(m.domain for m in with_facts).most_common(1)[0][0]
        best = next(m for m in with_facts if m.domain == dom)
    f = best.facts if best else {}

    def fact(name: str) -> str | None:
        return f.get(name, (None,))[0]

    directory = sorted((m for m in ms if m.source in NAME_SOURCES),
                       key=lambda m: NAME_SOURCES.index(m.source))
    dir_first = lambda attr: next((getattr(m, attr) for m in directory if getattr(m, attr)), None)  # noqa: E731

    name = fact("company_name") or dir_first("name")
    lf = fact("legal_form") or legal_form(name)
    if not lf and best:
        lf = "Einzelunternehmen"   # Impressum without a legal form: sole trader
    court, number = fact("register_court"), fact("register_number")
    if number and court:
        court = court if court.lower().startswith(("amtsgericht", "ag ")) else f"Amtsgericht {court}"
        register = f"{court} {number}"
    else:
        register = number
    dom = best.domain if best else Counter(m.domain for m in ms if m.domain).most_common(1)[0][0] \
        if any(m.domain for m in ms) else None
    final_url, impressum_url = crawl.get(dom, (None, None)) if dom else (None, None)
    if best and not impressum_url:
        impressum_url = next((u for _, u in f.values() if u), None)
    sources = sorted({m.source for m in ms})
    places = sorted({place_ids[m.cid] for m in ms if m.cid in place_ids})

    status, decided_by, reasons = verdict(ms)
    row: dict[str, Any] = {
        "company_id": hashlib.sha1(pk.encode()).hexdigest()[:12], "primary_key": pk,
        "company_name": name, "legal_form": lf,
        "street": fact("street") or dir_first("street"),
        "postcode": fact("postcode") or dir_first("postcode"),
        "city": fact("city") or dir_first("city"),
        "phone": fact("phone") or dir_first("phone"),
        "website": final_url, "impressum_url": impressum_url, "register_number": register,
        "sources": ";".join(sources), "source_count": len(sources),
        "google_place_id": ";".join(places) or None,
        "decided_by": decided_by, "last_checked": date.today(),
        "bezirk": None, "lat": None, "lon": None,
    }
    if status == "drop":
        row.update(status=None, tab="excluded", excluded_reason=reasons[0], review_reason=None)
        return row
    if not name:
        # Google-only (or nameless) cluster: no Impressum name, nothing non-Google to show
        row.update(status=None, tab="excluded", review_reason=None,
                   excluded_reason="no Impressum (Google Maps only)" if sources == ["serper_maps"] else "no company name")
        return row
    if any(m.housing for m in ms):
        reasons.append("housing company")
    for m in ms:
        reasons += m.reviews
    if not impressum_url:
        reasons.append("no Impressum found")
    if not row["phone"] and not row["website"]:
        reasons.append("no phone or website")
    reasons = list(dict.fromkeys(reasons))
    row.update(status="review" if reasons else "confirmed", review_reason="; ".join(reasons) or None,
               excluded_reason=None, tab="companies")
    return row


FINGERPRINT_FIELDS = ["company_name", "legal_form", "street", "postcode", "phone", "website",
                      "impressum_url", "register_number", "sources", "status"]


def run_dedupe(ctx: Ctx, params: dict[str, Any] | None = None) -> StageResult:
    res = StageResult(run_id=ctx.run_id, stage=STAGE)
    dc = ctx.cfg["dedupe"]
    if not ctx.con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'serper_maps_tmp'").fetchone()[0]:
        res.errors.append("serper_maps_tmp missing (dropped by export): run discover first")
        return res.finish("failed")
    members = load_members(ctx)
    clusters, merges = cluster(members, dc["name_min_ratio"], dc["max_names_per_phone"])
    place_ids = dict(ctx.con.execute("SELECT candidate_id, place_id FROM place_ref").fetchall())
    crawl = {d: (fu, iu) for d, fu, iu in ctx.con.execute(
        "SELECT domain, final_url, impressum_url FROM crawl_site WHERE status = 'ok'").fetchall()}

    prev = {cid: (fs, fp) for cid, fs, fp in ctx.con.execute(
        "SELECT company_id, first_seen_run, fingerprint FROM company").fetchall()}
    rows, member_rows = [], []
    seen: dict[str, dict] = {}
    for ms in clusters:
        row = build_company(ms, place_ids, crawl)
        if row["company_id"] in seen:   # two clusters with the same key (e.g. blocked register merge)
            row["company_id"] = hashlib.sha1((row["primary_key"] + "|" + min(m.cid for m in ms)).encode()).hexdigest()[:12]
        seen[row["company_id"]] = row
        fp = hashlib.sha1(json.dumps([str(row.get(k)) for k in FINGERPRINT_FIELDS]).encode()).hexdigest()[:12]
        first, old_fp = prev.get(row["company_id"], (ctx.run_id, None))
        row.update(fingerprint=fp, first_seen_run=first,
                   last_changed_run=ctx.run_id if fp != old_fp else None)
        rows.append(row)
        member_rows += [[row["company_id"], m.cid] for m in ms]
    # keep last_changed_run of unchanged companies
    old_changed = dict(ctx.con.execute("SELECT company_id, last_changed_run FROM company").fetchall())
    for r in rows:
        if r["last_changed_run"] is None:
            r["last_changed_run"] = old_changed.get(r["company_id"]) or ctx.run_id

    cols = [c[0] for c in ctx.con.execute("DESCRIBE company").fetchall()]
    ctx.con.execute("DELETE FROM company")
    ctx.con.execute("DELETE FROM company_member")
    ctx.con.executemany(f"INSERT INTO company VALUES ({', '.join('?' * len(cols))})",
                        [[r.get(c) for c in cols] for r in rows])
    ctx.con.executemany("INSERT INTO company_member VALUES (?, ?)", member_rows)

    tabs = Counter(r["tab"] for r in rows)
    status = Counter(r["status"] for r in rows if r["tab"] == "companies")
    res.counts = {
        "candidates": len(members),
        "clusters": len(clusters),
        "merged_records": len(members) - len(clusters),
        "merges_by_rule": merges,
        "companies": tabs["companies"],
        "confirmed": status["confirmed"],
        "review": status["review"],
        "excluded": tabs["excluded"],
        "excluded_google_only": sum(1 for r in rows if r.get("excluded_reason") == "no Impressum (Google Maps only)"),
        "new_companies": sum(1 for r in rows if r["company_id"] not in prev),
        "changed_companies": sum(1 for r in rows if r["company_id"] in prev and r["last_changed_run"] == ctx.run_id),
    }
    for k, v in res.counts.items():
        ctx.log(STAGE, k, v)
    return res.finish("ok")
