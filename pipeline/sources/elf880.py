"""11880.com result lists: /suche/<term>/<ort>?page=N, 50 per page.

robots.txt allows /suche. Listings carry no website (resolve stage finds it).
Module named elf880 because Python module names cannot start with a digit.
"""
from __future__ import annotations

from urllib.parse import quote

from selectolax.lexbor import LexborHTMLParser, LexborNode

from pipeline.http import Fetcher, Response
from pipeline.models import Candidate
from pipeline.normalize import clean_ws, phone_e164

BASE = "https://www.11880.com"
MAX_PAGES = 20


def parse_entry(li: LexborNode) -> Candidate | None:
    link = li.css_first("a.entry-detail-link")
    title = li.css_first(".result-list-entry-title__headline")
    if not link or not title:
        return None
    href = link.attributes.get("href") or ""
    addr = li.css_first(".result-list-entry-address")
    street = postcode = city = None
    if addr:
        s = addr.css_first("span.d-block")
        p = addr.css_first(".js-postal-code")
        c = addr.css_first(".js-address-locality")
        street = clean_ws(s.text()) if s else None
        postcode = clean_ws(p.text()) if p else None
        city = clean_ws(c.text()) if c else None
    tel = li.css_first("a[href^='tel:']")
    label = li.css_first(".result-list-entry-phone-number__label")
    phone = phone_e164(tel.attributes["href"][4:]) if tel else None
    if not phone and label:
        phone = phone_e164(label.text())
    trades = li.css_first(".trades-list")
    return Candidate(
        source="11880",
        source_ref=BASE + href if href.startswith("/") else href,
        name=clean_ws(title.text()),
        street=street,
        postcode=postcode,
        city=city,
        phone=phone,
        raw_category=clean_ws(trades.text()) if trades else None,
    )


def parse_html(html: str, page: int) -> tuple[list[Candidate], bool]:
    """Entries on one page + whether a next page exists."""
    tree = LexborHTMLParser(html)
    out = [c for li in tree.css("li.result-list-entry") if (c := parse_entry(li))]
    has_next = tree.css_first(f'.pagination a[data-page="{page + 1}"]') is not None
    return out, has_next


async def search(f: Fetcher, term: str, ort: str) -> tuple[list[Candidate], dict]:
    base = f"{BASE}/suche/{quote(term.lower())}/{quote(ort.lower())}"
    info: dict = {"url": base, "pages": 0}
    entries: list[Candidate] = []
    page = 1
    while page <= MAX_PAGES:
        url = base if page == 1 else f"{base}?page={page}"
        r = await f.fetch(url)
        if not isinstance(r, Response) or r.status != 200:
            info["error"] = getattr(r, "reason", None) or f"status {getattr(r, 'status', '?')}"
            break
        items, has_next = parse_html(r.text, page)
        info["pages"] += 1
        entries += items
        if not items or not has_next:
            break
        page += 1
    info["parsed"] = len(entries)
    return entries, info
