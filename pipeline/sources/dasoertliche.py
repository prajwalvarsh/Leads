"""Das Örtliche result lists: /?form_name=search_nat&kw=<term>&ci=<ort>, 25 per page via recFrom.

robots.txt allows everything. Listings carry no website (resolve stage finds it).
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse

from selectolax.lexbor import LexborHTMLParser, LexborNode

from pipeline.http import Fetcher, Response
from pipeline.models import Candidate
from pipeline.normalize import clean_ws, parse_postcode_city, phone_e164

BASE = "https://www.dasoertliche.de/"
MAX_PAGES = 80


def locations(bezirk: str) -> list[str]:
    """Das Örtliche does not know two-part Bezirk names ('Charlottenburg-Wilmersdorf') and then
    silently falls back to nationwide results. Query each part instead."""
    return [f"Berlin-{part}" for part in bezirk.split("-")]


def is_berlin(c: Candidate) -> bool:
    return bool(c.postcode and "10115" <= c.postcode <= "14199" and (c.city or "").startswith("Berlin"))


def stable_detail_url(href: str) -> str:
    """Drop per-search tracking params (kw, hitno, pagePos...) so the same entry gets one ref."""
    q = parse_qs(urlparse(href).query)
    keep = {k: q[k][0] for k in ("form_name", "id", "recuid") if k in q}
    return BASE + "?" + urlencode(keep)


def parse_hit(hit: LexborNode) -> Candidate | None:
    a = hit.css_first("a.hitlnk_name")
    if not a or not a.attributes.get("href"):
        return None
    street = postcode = city = None
    addr = hit.css_first("address")
    if addr:
        lines = [clean_ws(x) for x in addr.text(separator="\n").split("\n")]
        lines = [x for x in lines if x]
        for i, line in enumerate(lines):
            pc, ct = parse_postcode_city(line)
            if pc:
                postcode, city = pc, ct
                street = lines[i - 1] if i > 0 else None
                break
    phone = hit.css_first(".phoneblock span")
    cat = hit.css_first(".category")
    return Candidate(
        source="dasoertliche",
        source_ref=stable_detail_url(a.attributes["href"]),
        name=clean_ws(a.text()),
        street=street,
        postcode=postcode,
        city=city,
        phone=phone_e164(phone.text()) if phone else None,
        raw_category=clean_ws(cat.text()) if cat else None,
    )


def parse_html(html: str) -> tuple[list[Candidate], str | None]:
    """Entries on one page + URL of the next page (or None)."""
    tree = LexborHTMLParser(html)
    out = [c for h in tree.css("div.hit") if (c := parse_hit(h))]
    nxt = tree.css_first('.paging a[title="zur nächsten Seite"]')
    return out, (nxt.attributes.get("href") if nxt else None)


async def search(f: Fetcher, term: str, ort: str) -> tuple[list[Candidate], dict]:
    url = BASE + "?" + urlencode(
        {"form_name": "search_nat", "kw": term, "ci": ort, "kgs": "", "buab": "", "zbuab": ""}
    )
    info: dict = {"url": url, "pages": 0}
    entries: list[Candidate] = []
    while url and info["pages"] < MAX_PAGES:
        r = await f.fetch(url)
        if not isinstance(r, Response) or r.status != 200:
            info["error"] = getattr(r, "reason", None) or f"status {getattr(r, 'status', '?')}"
            break
        page, url = parse_html(r.text)
        info["pages"] += 1
        entries += page
        # Results past the location spill into other cities: stop at the first page without Berlin.
        if not page or not any(is_berlin(c) for c in page):
            break
    info["parsed"] = len(entries)
    return entries, info
