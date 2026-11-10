"""Task 2, stage kpi_extract: one LLM call per confirmed company over its crawled pages.

Every field comes back as {value, quote}. Evidence check (same as Task 1): the quote must be
verbatim in one of the company's pages (whitespace-normalised) and must contain or support the
value. A value that fails the check is left empty, never guessed. Empty answers ("no",
"not named", empty list) mean "not found in the crawled pages".

Email: a personal address must appear on the company's pages and must not be a role address
(info@, kontakt@, ...). If the decision maker's own address is not published but another person's
address at the company domain shows a pattern (j.schmidt@), the pattern is applied to the decision
maker if the domain has an MX record -> email_basis = pattern.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from pipeline.budget import Budget, CapExceeded
from pipeline.contract import Ctx, StageResult
from pipeline.extract import norm
from pipeline.llm import LLM, load_prompt
from pipeline.normalize import fold

STAGE = "kpi_extract"
PAGE_ORDER = ["impressum", "services", "about", "team", "references", "jobs", "home"]
SEGMENTS = ["WEG", "Miet", "SE", "Gewerbe"]
KPI_FIELDS = ["units_managed", "segments", "is_housing_company", "metering_provider", "remote_reading_mentioned",
              "digital_tools", "decision_maker", "personal_email"]

SUPPORT = {   # a quote must contain one of these (folded) to support the value
    "WEG": ["weg", "wohnungseigentum", "eigentuemergemeinschaft", "eigentumswohnung", "eigentuemerversammlung"],
    # not bare "miet": "Vermietung Ihrer Immobilie" is letting (Makler work), not rental management
    "Miet": ["mietverwalt", "mietshaus", "mietshaeuser", "mietobjekt", "mieteinheit", "mietwohn", "mietimmobil",
             "mieterbetreuung", "mietverhaeltnis", "zinshaus", "zinshaeuser", "mehrfamilienhaus", "mehrfamilienhaeuser"],
    "SE": ["sondereigentum", "se-verwaltung"],
    "Gewerbe": ["gewerb"],
    "remote_reading_mentioned": ["funk", "fernablesung", "fernauslesung", "fernabgelesen", "hkvo",
                                 "heizkostenverordnung", "unterjaehrig"],
    "digital_tools": ["portal", "app", "software", "online", "casavi", "domus", "karthago", "immoware",
                      "fairwalter", "aareon", "wodis", "gfad", "facilioo", "etg24"],
    "is_housing_company": ["genossenschaft", "wohnungsbau", "wohnungsunternehmen", "wohnungsgesellschaft",
                           "eigenen bestand", "eigenbestand"],
}


def _vq(value: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["value", "quote"],
            "properties": {"value": value, "quote": {"type": "string"}}}


_STR = {"type": ["string", "null"]}
_YN = {"type": "string", "enum": ["yes", "no"]}
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": KPI_FIELDS,
    "properties": {
        "units_managed": _vq(_STR),
        "segments": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["segment", "quote"],
            "properties": {"segment": {"type": "string", "enum": SEGMENTS}, "quote": {"type": "string"}}}},
        "is_housing_company": _vq(_YN),
        "metering_provider": _vq({"type": "string"}),
        "remote_reading_mentioned": _vq(_YN),
        "digital_tools": _vq(_YN),
        "decision_maker": {"type": "object", "additionalProperties": False, "required": ["name", "role", "quote"],
                           "properties": {"name": _STR, "role": _STR, "quote": {"type": "string"}}},
        "personal_email": _vq(_STR),
    },
}


# ---------- text ----------
def build_text(pages: dict[str, tuple[str, str]], max_chars: int) -> str:
    """Pages in a fixed order, each with a header line; every page gets a fair share of the budget."""
    present = [k for k in PAGE_ORDER if k in pages]
    parts, left = [], max_chars
    for i, k in enumerate(present):
        url, text = pages[k]
        chunk = text[: max(max_chars // len(present), left // (len(present) - i))]
        left -= len(chunk)
        parts.append(f"### {k} {url}\n{chunk}")
    return "\n\n".join(parts)


# ---------- evidence ----------
def _nullish(v: Any) -> Any:
    return None if isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a") else v


def find_page(quote: str | None, pages_by_url: dict[str, str]) -> str | None:
    q = norm(quote)
    if not q:
        return None
    return next((u for u, t in pages_by_url.items() if q in t), None)


def supports(key: str, quote: str) -> bool:
    fq = fold(quote)
    return any(w in fq for w in SUPPORT[key])


_UNIT_WORDS = ("wohnung", "einheit", "apartment", "appartement", "mietobjekt")
_NOT_UNITS = re.compile(r"€|\beur\b|euro|m²|m2\b|\bqm\b|quadratmeter|saniert|verkauft|vermittelt|gebaut", re.I)


def is_unit_quote(quote: str | None) -> bool:
    """A unit count needs a unit word (Wohnungen, Einheiten) and no money, area or sold/renovated context:
    'Immobilienvermögen über 1.500.000.000,00 EUR' and '5.000 Wohnungen saniert' are not managed units."""
    q = fold(quote or "")
    return any(w in q for w in _UNIT_WORDS) and not _NOT_UNITS.search(quote or "")


def parse_number(value: str | None) -> int | None:
    """'über 3.500' -> 3500; 'rund 1200' -> 1200."""
    if not value:
        return None
    m = re.search(r"\d{1,3}(?:[. ]\d{3})+|\d+", value)
    return int(re.sub(r"[. ]", "", m.group(0))) if m else None


# ---------- email ----------
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_AT = re.compile(r"\s*[\(\[\{]\s*(?:at|ät)\s*[\)\]\}]\s*|\s+(?:at|ät)\s+", re.I)
_DOT = re.compile(r"\s*[\(\[\{]\s*(?:dot|punkt)\s*[\)\]\}]\s*", re.I)


def emails_in(text: str) -> set[str]:
    t = _DOT.sub(".", _AT.sub("@", text))
    return {e.lower().rstrip(".") for e in EMAIL.findall(t)}


def is_role_address(email: str, role_prefixes: list[str]) -> bool:
    local = email.split("@")[0].lower()
    parts = re.split(r"[._\-+]", local)
    return local in role_prefixes or parts[0] in role_prefixes or any(local.startswith(p) and len(local) <= len(p) + 2
                                                                      for p in role_prefixes)


def name_parts(name: str) -> tuple[str, str] | None:
    """'Dipl.-Kfm. Jürgen Schmidt-Weiß' -> ('juergen', 'schmidt-weiss')."""
    titles = r"\b(dr|prof|dipl|ing|kfm|kffr|kauffrau|kaufmann|herr|frau|mba|ll\.?m|med|rer|pol|oec|b\.?a|m\.?a)\b\.?"
    words = [w for w in re.sub(titles, " ", fold(name)).replace(".", " ").split() if re.fullmatch(r"[a-z][a-z\-]*", w)]
    if len(words) < 2:
        return None
    return words[0], words[-1]


PATTERNS = {
    "first.last": lambda f, l: f"{f}.{l}", "f.last": lambda f, l: f"{f[0]}.{l}", "last": lambda f, l: l,
    "firstlast": lambda f, l: f"{f}{l}", "flast": lambda f, l: f"{f[0]}{l}", "first": lambda f, l: f,
    "last.first": lambda f, l: f"{l}.{f}", "first_last": lambda f, l: f"{f}_{l}", "first-last": lambda f, l: f"{f}-{l}",
}


_EXTRA_OK = {"", "hv", "hausverw", "hausverwaltung", "immo", "immobilien", "verwaltung", "berlin"}


def matches_person(email: str, name: str) -> bool:
    """The address belongs to this person: a known pattern, or the surname plus nothing that names
    someone else (only the person's first name or initial, or 'hv'/'hausverwaltung' may be added).
    'tina.grieben@' is not Björn Grieben's address."""
    np = name_parts(name)
    if not np:
        return False
    first, last = np
    local = email.split("@")[0].lower()
    if any(local == p(first, last) for p in PATTERNS.values()):
        return True
    compact, surname = re.sub(r"[._\-]", "", local), last.replace("-", "")
    if surname not in compact:
        return False
    rest = compact.replace(surname, "", 1)
    return rest in _EXTRA_OK or rest in (first.replace("-", ""), first[0])


def infer_pattern(email: str, people: list[str]) -> str | None:
    """Pattern of a personal address, from a person named on the pages whose name it matches."""
    local = email.split("@")[0].lower()
    for person in people:
        np = name_parts(person)
        if not np:
            continue
        for key, fn in PATTERNS.items():
            if key != "first" and local == fn(*np):
                return key
    return None


def has_mx(domain: str) -> bool:
    try:
        import dns.resolver
        return bool(dns.resolver.resolve(domain, "MX", lifetime=10))
    except Exception:
        return False


# every pair of adjacent capitalised words (overlapping: "Ansprechpartnerin Anna Weber" -> both pairs)
_PERSON = re.compile(r"(?=\b([A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?)\s+([A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?)\b)")


def people_near(text: str, email: str) -> list[str]:
    """Person-like names in the 120 characters before an address (team pages: name, then email)."""
    i = text.lower().find(email.split("@")[0])
    window = text[max(0, i - 120): i] if i >= 0 else ""
    return [f"{a} {b}" for a, b in _PERSON.findall(window)]


def email_for(dm_name: str | None, llm_email: str | None, pages_by_url: dict[str, str], domain: str,
              role_prefixes: list[str]) -> tuple[str | None, str | None, str | None, str | None]:
    """-> (email, basis, quote, url)."""
    found: dict[str, str] = {}
    for url, text in pages_by_url.items():
        for e in emails_in(text):
            found.setdefault(e, url)
    personal = {e: u for e, u in found.items() if not is_role_address(e, role_prefixes)}
    llm_email = (llm_email or "").lower().strip()
    # 1. published: on the pages and matching the decision maker
    if dm_name:
        for e, u in personal.items():
            if matches_person(e, dm_name):
                return e, "published", e, u
    if llm_email and llm_email in personal and not dm_name:
        return llm_email, "published", llm_email, personal[llm_email]
    # 2. pattern: another person's address at the company domain
    if dm_name and name_parts(dm_name):
        for e, u in personal.items():
            if not e.endswith("@" + domain):
                continue
            key = infer_pattern(e, people_near(pages_by_url[u], e))
            if key and has_mx(domain):
                guess = f"{PATTERNS[key](*name_parts(dm_name))}@{domain}"
                return guess, "pattern", f"pattern {key} from {e}", u
    return None, None, None, None


# ---------- one company ----------
def evaluate(out: dict, pages_by_url: dict[str, str], domain: str, role_prefixes: list[str]) -> tuple[dict, list[list], dict]:
    """-> (kpi row, evidence rows [field, value, quote, url], stats per field: kept / rejected / empty)."""
    row: dict[str, Any] = {}
    ev: list[list] = []
    stats: dict[str, str] = {}

    def keep(field: str, value: Any, quote: str, url: str) -> None:
        row[field] = value
        ev.append([field, str(value), norm(quote), url])
        stats[field] = "kept"

    # units
    u = out.get("units_managed") or {}
    val, url = _nullish(u.get("value")), find_page(u.get("quote"), pages_by_url)
    n = parse_number(val)
    row["units_managed"] = None
    stats["units_managed"] = "empty" if not val else "rejected"
    if n and url and str(n) in re.sub(r"\D", "", norm(u.get("quote"))) and is_unit_quote(u.get("quote")):
        keep("units_managed", n, u["quote"], url)
        row["units_managed_text"] = val

    # segments: one quote per segment
    segs, stats["segments"] = [], "empty" if not out.get("segments") else "rejected"
    for s in out.get("segments") or []:
        url = find_page(s.get("quote"), pages_by_url)
        if url and s.get("segment") in SEGMENTS and supports(s["segment"], s["quote"]) and s["segment"] not in segs:
            segs.append(s["segment"])
            ev.append(["segments", s["segment"], norm(s["quote"]), url])
    row["segments"] = ", ".join(segs) or None
    if segs:
        stats["segments"] = "kept"

    # yes/no fields: "yes" needs a supporting quote on a page; otherwise "no"
    for f in ("is_housing_company", "remote_reading_mentioned", "digital_tools"):
        o = out.get(f) or {}
        row[f], stats[f] = "no", "empty"
        if o.get("value") == "yes":
            url = find_page(o.get("quote"), pages_by_url)
            if url and supports(f, o["quote"]):
                keep(f, "yes", o["quote"], url)
            else:
                stats[f] = "rejected"

    # metering provider: the name must be in the quote
    o = out.get("metering_provider") or {}
    name = _nullish(o.get("value"))
    row["metering_provider"], stats["metering_provider"] = "not named", "empty"
    if name and name.lower() != "not named":
        url = find_page(o.get("quote"), pages_by_url)
        if url and fold(name.split()[0]) in fold(o["quote"]):
            keep("metering_provider", name, o["quote"], url)
        else:
            stats["metering_provider"] = "rejected"

    # decision maker: name in the quote, quote on a page
    d = out.get("decision_maker") or {}
    dm, role = _nullish(d.get("name")), _nullish(d.get("role"))
    row["decision_maker"], row["decision_maker_role"], stats["decision_maker"] = None, None, "empty"
    if dm:
        url = find_page(d.get("quote"), pages_by_url)
        if url and norm(dm) in norm(d["quote"]):
            keep("decision_maker", norm(dm), d["quote"], url)
            row["decision_maker_role"] = norm(role) if role else None
        else:
            stats["decision_maker"] = "rejected"

    # personal email
    e = out.get("personal_email") or {}
    email, basis, quote, url = email_for(row["decision_maker"], _nullish(e.get("value")), pages_by_url, domain, role_prefixes)
    row["personal_email"], row["email_basis"] = email, basis
    stats["personal_email"] = "kept" if email else ("rejected" if _nullish(e.get("value")) else "empty")
    if email:
        ev.append(["personal_email", email, quote, url])
    row.setdefault("units_managed_text", None)
    row["fields_with_evidence"] = sum(1 for f in KPI_FIELDS if stats[f] == "kept")
    return row, ev, stats


# ---------- stage ----------
def company_pages(ctx: Ctx) -> tuple[dict[str, dict[str, tuple[str, str]]], dict[str, str]]:
    """company_id -> {page_type: (url, text)}, company_id -> registrable domain."""
    rows = ctx.con.execute(
        """SELECT co.company_id, s.domain, p.page_type, p.url, p.text
           FROM company co
           JOIN crawl_site s ON s.final_url = co.website AND s.status = 'ok'
           JOIN page p ON p.domain = s.domain
           WHERE co.tab = 'companies' AND co.status = 'confirmed' AND p.text IS NOT NULL
           ORDER BY co.company_id, p.url"""
    ).fetchall()
    pages: dict[str, dict[str, tuple[str, str]]] = {}
    domains: dict[str, str] = {}
    for cid, dom, kind, url, text in rows:
        pages.setdefault(cid, {}).setdefault(kind, (url, text))
        domains.setdefault(cid, dom)
    return pages, domains


KPI_COLS = ["company_id", "units_managed", "units_managed_text", "segments", "is_housing_company", "metering_provider",
            "remote_reading_mentioned", "digital_tools", "decision_maker", "decision_maker_role", "personal_email",
            "email_basis", "fields_with_evidence", "run_id"]


def run_kpi_extract(ctx: Ctx, params: dict[str, Any] | None = None) -> StageResult:
    params = params or {}
    res = StageResult(run_id=ctx.run_id, stage=STAGE)
    pages, domains = company_pages(ctx)
    ids = sorted(pages)
    if params.get("company_ids"):
        ids = [i for i in ids if i in set(params["company_ids"])]
    if ctx.mode == "incremental":
        done = {r[0] for r in ctx.con.execute("SELECT company_id FROM kpi").fetchall()}
        ids = [i for i in ids if i not in done]
    names = dict(ctx.con.execute("SELECT company_id, company_name FROM company").fetchall())
    max_chars = ctx.cfg["llm"]["max_text_chars"]
    users = [f"Company: {names.get(i)}\n\nWebsite text:\n<<<\n{build_text(pages[i], max_chars)}\n>>>" for i in ids]

    caps = ctx.cfg["caps"]
    budget = Budget(ctx.con, "llm_kpi", caps["llm_kpi"], caps["llm_month"])
    try:
        llm = LLM(ctx.cfg["models"]["kpi"], budget, ctx.cfg["llm"]["temperature"], max_retries=1)
        print(f"[kpi_extract] {len(ids)} companies, model {llm.model}")
        outs = llm.run_many(load_prompt("kpi", ctx.cfg["prompts"]["kpi"]), users, "kpis", SCHEMA,
                            ctx.cfg["llm"]["concurrency"])
    except (RuntimeError, CapExceeded) as e:
        res.errors.append(str(e))
        return res.finish("failed")

    roles = ctx.cfg["kpi"]["role_email_prefixes"]
    stats_all: dict[str, Counter[str]] = {f: Counter() for f in KPI_FIELDS}
    rows, ev_rows, failed = [], [], 0
    for cid, out in zip(ids, outs):
        if isinstance(out, Exception):
            failed += 1
            res.errors.append(f"{cid}: {type(out).__name__}: {str(out)[:150]}")
            continue
        by_url = {u: norm(t) for u, t in pages[cid].values()}
        row, ev, stats = evaluate(out, by_url, domains[cid], roles)
        for f, s in stats.items():
            stats_all[f][s] += 1
        rows.append([cid] + [row.get(c) for c in KPI_COLS[1:-1]] + [ctx.run_id])
        ev_rows += [[cid] + e for e in ev]

    if rows:
        ctx.con.execute("CREATE TEMP TABLE IF NOT EXISTS _kpi (company_id VARCHAR)")
        ctx.con.execute("DELETE FROM _kpi")
        ctx.con.executemany("INSERT INTO _kpi VALUES (?)", [[r[0]] for r in rows])
        ctx.con.execute("DELETE FROM kpi WHERE company_id IN (SELECT company_id FROM _kpi)")
        ctx.con.execute("DELETE FROM kpi_evidence WHERE company_id IN (SELECT company_id FROM _kpi)")
        ctx.con.executemany(f"INSERT INTO kpi VALUES ({', '.join('?' * len(KPI_COLS))})", rows)
    if ev_rows:
        ctx.con.executemany("INSERT INTO kpi_evidence VALUES (?, ?, ?, ?, ?)", ev_rows)

    n = len(rows) or 1
    basis = Counter(r[KPI_COLS.index("email_basis")] for r in rows if r[KPI_COLS.index("email_basis")])
    res.counts = {
        "companies": len(ids),
        "fill_rate_with_evidence": {f: f"{stats_all[f]['kept']}/{len(rows)} ({100 * stats_all[f]['kept'] // n}%)" for f in KPI_FIELDS},
        "rejected_by_evidence_check": {f: stats_all[f]["rejected"] for f in KPI_FIELDS if stats_all[f]["rejected"]},
        "email_basis": dict(basis),
        "llm_calls": llm.calls,
        "llm_cache_hits": llm.cache_hits,
        "llm_failed_calls": failed,
        "tokens_in": llm.tokens_in,
        "tokens_out": llm.tokens_out,
    }
    res.api_calls["llm_kpi"] = budget.used
    for k, v in res.counts.items():
        ctx.log(STAGE, k, v)
    res.errors = res.errors[:20]
    return res.finish("failed" if failed > max(5, len(ids) // 10) else "ok")
