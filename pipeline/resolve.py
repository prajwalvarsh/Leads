"""Stage 2: resolve one website per candidate.

Order (docs/task1.md + budget rules agreed with the user):
1. The candidate's own source website (serper_maps: serper_maps_tmp.website).
2. A website from another source's record of the same company. Same company = same domain,
   same phone, or fuzzy name >= group_min_ratio at the same street (the overlap rules).
3. Serper web search `"<name>" Berlin Impressum`, one search per company group, only for groups
   that still have no website. Order: found by 2+ sources, then name has a property-management
   keyword, then the rest. Accept the first result whose domain matches the name
   (token_set_ratio >= domain_min_ratio); shared hosts and portals never count.
   Cap: remaining Serper credits minus resolve.serper_reserve_credits.

Searches are never repeated: candidates already searched (method search/none) keep their result.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from pipeline.budget import Budget, CapExceeded
from pipeline.contract import Ctx, StageResult
from pipeline.db import now
from pipeline.normalize import fold, normalise_name, phone_e164, site_root
from pipeline.overlap import domain_of, same_street, street_key
from pipeline.sources.serper_maps import SerperClient

STAGE = "resolve"
MAX_NAMES_PER_KEY = 3   # a phone/domain used by more different names than this links nothing
# Source order for picking the name used in the search query
NAME_PRIORITY = ["gelbeseiten", "dasoertliche", "osm", "11880", "association", "search", "serper_maps"]
# Words that say nothing about which company a domain belongs to
GENERIC = {
    "hausverwaltung", "hausverwaltungen", "immobilienverwaltung", "immobilienverwaltungen", "verwaltung",
    "verwaltungen", "immobilien", "immobilie", "mietverwaltung", "weg", "hv", "berlin", "und", "gmbh",
    "grundstuecksverwaltung", "hausverwalter", "service", "services", "management", "real", "estate",
    "property", "de", "the", "der", "die", "das", "von", "fuer",
}


@dataclass
class Item:
    cid: str
    source: str
    name: str | None
    website: str | None   # source URL, if any (excluded hosts already removed)
    domain: str | None
    phone: str | None
    norm_name: str
    street: tuple[str, str] | None
    google: bool          # fields came from serper_maps_tmp


@dataclass
class Group:
    items: list[Item] = field(default_factory=list)

    @property
    def sources(self) -> set[str]:
        return {i.source for i in self.items}


# ---------- loading ----------
def load_items(ctx: Ctx, excluded: list[str]) -> tuple[list[Item], int]:
    """All candidates with the fields needed here. Returns (items, serper rows without tmp data)."""
    items: list[Item] = []
    for cid, src, name, street, phone, web in ctx.con.execute(
        "SELECT candidate_id, source, name, street, phone, website FROM candidate WHERE source <> 'serper_maps'"
    ).fetchall():
        d = domain_of(web, excluded)
        items.append(Item(cid, src, name, web if d else None, d, phone_e164(phone), normalise_name(name),
                          street_key(street), False))
    rows = ctx.con.execute(
        """SELECT c.candidate_id, t.name, t.address, t.phone, t.website
           FROM candidate c LEFT JOIN serper_maps_tmp t ON t.ref = c.source_ref
           WHERE c.source = 'serper_maps'"""
    ).fetchall()
    no_data = 0
    for cid, name, addr, phone, web in rows:
        if name is None:
            no_data += 1
            continue
        d = domain_of(web, excluded)
        items.append(Item(cid, "serper_maps", name, web if d else None, d, phone_e164(phone),
                          normalise_name(name), street_key(addr.split(",")[0] if addr else None), True))
    return items, no_data


# ---------- grouping ----------
def group_items(items: list[Item], name_min: int) -> list[Group]:
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    by_domain: dict[str, list[int]] = defaultdict(list)
    by_phone: dict[str, list[int]] = defaultdict(list)
    by_street: dict[str, list[int]] = defaultdict(list)
    for i, it in enumerate(items):
        if it.domain:
            by_domain[it.domain].append(i)
        if it.phone:
            by_phone[it.phone].append(i)
        if it.street and it.norm_name:
            by_street[it.street[0]].append(i)
    for bucket in list(by_domain.values()) + list(by_phone.values()):
        if len({items[i].norm_name for i in bucket}) > MAX_NAMES_PER_KEY:
            continue   # shared switchboard / franchise domain: not one company
        for j in bucket[1:]:
            union(bucket[0], j)
    for bucket in by_street.values():
        for x in range(len(bucket)):
            for y in range(x + 1, len(bucket)):
                a, b = items[bucket[x]], items[bucket[y]]
                if same_street(a.street, b.street) and fuzz.token_set_ratio(a.norm_name, b.norm_name) >= name_min:
                    union(bucket[x], bucket[y])
    groups: dict[int, Group] = defaultdict(Group)
    for i, it in enumerate(items):
        groups[find(i)].items.append(it)
    return list(groups.values())


# ---------- search ----------
def has_keyword(name: str | None, keywords: list[str]) -> bool:
    """Case-insensitive for words like Hausverwaltung; acronyms like WEG must be a capitalised
    word, so names like "Wegner" do not count."""
    if not name:
        return False
    for k in keywords:
        if k.isupper():
            if re.search(rf"\b{re.escape(k)}\b", name):
                return True
        elif k.lower() in name.lower():
            return True
    return False


def tier(g: Group, keywords: list[str]) -> int:
    """0 = found by 2+ sources, 1 = property-management keyword in a name, 2 = rest."""
    if len(g.sources) >= 2:
        return 0
    if any(has_keyword(i.name, keywords) for i in g.items):
        return 1
    return 2


def query_name(g: Group) -> str | None:
    named = [i for i in g.items if i.name]
    if not named:
        return None
    named.sort(key=lambda i: NAME_PRIORITY.index(i.source) if i.source in NAME_PRIORITY else 99)
    return named[0].name


def _core(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", fold(text)) if t and t not in GENERIC]


def domain_score(name: str, domain: str) -> int:
    """How well a domain matches a company name, 0-100. Generic words (Hausverwaltung, Berlin, ...)
    are removed first, so 'hausverwaltung-berlin.de' does not match every Hausverwaltung."""
    label = domain.rsplit(".", 1)[0]
    n, d = _core(normalise_name(name)), _core(label)
    if not n or not d:
        return 0
    score = int(fuzz.token_set_ratio(" ".join(n), " ".join(d)))
    compact_name, compact_dom = "".join(n), "".join(d)
    if len(compact_name) >= 5 and compact_name in compact_dom:   # domains without hyphens: hvmueller.de
        score = 100
    return score


def pick_result(name: str, data: dict, excluded: list[str], min_ratio: int) -> tuple[str, str, int] | None:
    """First organic result whose domain matches the name -> (site root URL, domain, score)."""
    for r in data.get("organic") or []:
        link = r.get("link")
        d = domain_of(link, excluded)
        if not d:
            continue
        s = domain_score(name, d)
        if s >= min_ratio:
            return site_root(link), d, s
    return None


# ---------- stage ----------
def run_resolve(ctx: Ctx, params: dict[str, Any] | None = None) -> StageResult:
    params = params or {}
    res = StageResult(run_id=ctx.run_id, stage=STAGE)
    rc = ctx.cfg["resolve"]
    excluded = ctx.cfg["shared_hosts"] + ctx.cfg["portal_domains"]
    kw: list[str] = rc["priority_keywords"]

    items, no_data = load_items(ctx, excluded)
    existing = {cid: (method, url, domain) for cid, method, url, domain in ctx.con.execute(
        "SELECT candidate_id, method, url, domain FROM website").fetchall()}
    groups = group_items(items, rc["group_min_ratio"])

    rows: dict[str, tuple] = {}   # candidate_id -> (url, domain, method, detail, from_google)
    to_search: list[Group] = []
    for g in groups:
        own = [i for i in g.items if i.website]
        if own:
            # most common domain in the group; its URL from a non-Google source if possible
            top = Counter(i.domain for i in own).most_common(1)[0][0]
            donor = sorted((i for i in own if i.domain == top), key=lambda i: i.google)[0]
            for i in g.items:
                if i.website:
                    rows[i.cid] = (i.website, i.domain, "source", i.source, i.google)
                else:
                    rows[i.cid] = (donor.website, donor.domain, "group", donor.cid, donor.google)
            continue
        searched = [existing[i.cid] for i in g.items if existing.get(i.cid, ("",))[0] in ("search", "none")]
        if searched:   # never pay twice: reuse the earlier search result for the whole group
            method, url, domain = searched[0]
            for i in g.items:
                rows[i.cid] = (url, domain, method, "earlier search", False)
            continue
        to_search.append(g)

    if ctx.mode == "incremental":   # only candidates without a website row yet
        rows = {cid: r for cid, r in rows.items() if cid not in existing}
        to_search = [g for g in to_search if any(i.cid not in existing for i in g.items)]

    to_search.sort(key=lambda g: (tier(g, kw), min(i.cid for i in g.items)))
    tiers = Counter(tier(g, kw) for g in to_search)

    # Search cap: remaining Serper credits minus the reserve
    total = ctx.cfg["caps"]["serper_total"]
    used_all = int(ctx.con.execute("SELECT coalesce(sum(calls), 0) FROM api_usage WHERE api = 'serper'").fetchone()[0])
    search_cap = max(0, total - used_all - rc["serper_reserve_credits"])
    budget = Budget(ctx.con, "serper", search_cap, total, all_time=True)
    print(f"[resolve] {len(items)} candidates in {len(groups)} groups; {len(to_search)} groups need a search "
          f"(2+ sources {tiers[0]}, keyword {tiers[1]}, rest {tiers[2]}); search cap {search_cap} credits")

    searches = hits = 0
    searched_tiers: Counter[int] = Counter()
    not_searched: list[Group] = []
    client = SerperClient(budget) if to_search and search_cap > 0 else None
    for n, g in enumerate(to_search):
        name = query_name(g)
        if client is None or budget.remaining() < 1 or not name:
            not_searched.append(g)
            continue
        q = rc["query"].format(name=name)
        try:
            data = client.search(q)
        except CapExceeded:
            not_searched.extend(to_search[n:])
            break
        except RuntimeError as e:   # Serper error: stop searching, keep what we have
            res.errors.append(f"search: {e}")
            not_searched.extend(to_search[n:])
            break
        searches += 1
        searched_tiers[tier(g, kw)] += 1
        pick = pick_result(name, data, excluded, rc["domain_min_ratio"])
        if pick:
            hits += 1
            url, domain, score = pick
            for i in g.items:
                rows[i.cid] = (url, domain, "search", f"{q} | score {score}", False)
        else:
            for i in g.items:
                rows[i.cid] = (None, None, "none", q, False)
        if searches % 100 == 0:
            print(f"[resolve] {searches} searches, {hits} websites found, {budget.used} credits")

    ts = now()
    ctx.con.executemany(
        "INSERT OR REPLACE INTO website VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [[cid, url, dom, method, detail, google, ctx.run_id, ts]
         for cid, (url, dom, method, detail, google) in rows.items()],
    )

    by_method = Counter(r[2] for r in rows.values())
    with_site = sum(1 for r in rows.values() if r[0])
    left_cap = sum(len(g.items) for g in not_searched)
    res.counts = {
        "candidates": len(items) + no_data,
        "groups": len(groups),
        "multi_source_groups": sum(1 for g in groups if len(g.sources) >= 2),
        "with_website": with_site,
        "with_website_by_method": {m: by_method[m] for m in ("source", "group", "search") if by_method[m]},
        "without_website": len(items) + no_data - with_site,
        "without_website_searched_no_match": by_method["none"],
        "without_website_not_searched_cap": left_cap,
        "without_website_no_serper_data": no_data,
        "groups_needing_search": len(to_search),
        "searches": searches,
        "searches_by_tier": {"2+_sources": searched_tiers[0], "keyword": searched_tiers[1], "rest": searched_tiers[2]},
        "search_hits": hits,
        "search_cap_credits": search_cap,
        "from_google": sum(1 for r in rows.values() if r[0] and r[4]),
        "place_details_calls": 0,   # Places disabled
    }
    res.api_calls["serper_credits"] = budget.used
    for k, v in res.counts.items():
        ctx.log(STAGE, k, v)
    ctx.log(STAGE, "api.serper_credits", budget.used)
    return res.finish("failed" if res.errors else "ok")
