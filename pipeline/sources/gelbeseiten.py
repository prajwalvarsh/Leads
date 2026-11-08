"""Gelbe Seiten listing pages: /branchen/<term>/<ort> (robots.txt disallows /suche, allows these).

First page shows up to 50 entries; more via POST /ajaxsuche (JSON with an "html" field).
"""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import quote

from selectolax.lexbor import LexborHTMLParser, LexborNode

from pipeline.http import Fetcher, Response
from pipeline.models import Candidate
from pipeline.normalize import clean_ws, parse_postcode_city, phone_e164

BASE = "https://www.gelbeseiten.de"
PAGE_SIZE = 10  # what the site's own "Mehr Anzeigen" button loads


def _b64(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def parse_article(a: LexborNode) -> Candidate | None:
    """source_ref = the entry's listing URL (gsbiz detail page)."""
    name_node = a.css_first("h2.mod-Treffer__name")
    link = a.css_first("a[href*='/gsbiz/']")
    ref = link.attributes.get("href") if link else None
    if not name_node or not ref:
        return None
    addr = a.css_first(".mod-AdresseKompakt__adress-text")
    street = postcode = city = None
    if addr:
        # e.g. "Jagowstr. 13, (Moabit)" -> "Jagowstr. 13"
        street = re.sub(r",?\s*\([^)]*\)\s*$", "", clean_ws(addr.text(deep=False)))
        street = street.rstrip(",").strip() or None
        ort = addr.css_first(".mod-AdresseKompakt__adress__ort")
        postcode, city = parse_postcode_city(ort.text() if ort else None)
    phone = a.css_first(".mod-TelefonnummerKompakt__phoneNumber")
    web = a.css_first("[data-webseitelink]")  # parser lowercases attribute names
    cat = a.css_first(".mod-Treffer--besteBranche")
    return Candidate(
        source="gelbeseiten",
        source_ref=ref,
        name=clean_ws(name_node.text()),
        street=street,
        postcode=postcode,
        city=city,
        phone=phone_e164(phone.text()) if phone else None,
        website=_b64(web.attributes.get("data-webseitelink")) if web else None,
        raw_category=clean_ws(cat.text()) if cat else None,
    )


def parse_html(html: str) -> list[Candidate]:
    tree = LexborHTMLParser(html)
    return [c for a in tree.css("article.mod-Treffer") if (c := parse_article(a))]


def _input(tree: LexborHTMLParser, ident: str) -> str | None:
    n = tree.css_first(f"#{ident}")
    return n.attributes.get("value") if n else None


async def search(f: Fetcher, term: str, ort: str) -> tuple[list[Candidate], dict]:
    """All entries for one term + location. Returns (entries, info)."""
    url = f"{BASE}/branchen/{quote(term.lower())}/{quote(ort.lower())}"
    r = await f.fetch(url)
    info: dict = {"url": url}
    if not isinstance(r, Response) or r.status != 200:
        info["error"] = getattr(r, "reason", None) or f"status {getattr(r, 'status', '?')}"
        return [], info
    tree = LexborHTMLParser(r.text)
    total_node = tree.css_first("#mod-TrefferlisteInfo")
    total = int(total_node.text().strip()) if total_node and total_node.text().strip().isdigit() else 0
    where = tree.css_first("#woBegriff")
    info.update(total=total, where=clean_ws(where.text()) if where else None, final_url=r.final_url)
    entries = parse_html(r.text)

    wo = _input(tree, "loadMore_WO")
    was = _input(tree, "loadMore_WAS")
    position = len(entries) + 1
    while wo and was and position <= total:
        form = {
            "umkreis": "-1", "verwandt": "false", "WAS": was, "WO": wo,
            "position": str(position), "anzahl": str(PAGE_SIZE), "sortierung": "relevanz",
        }
        r2 = await f.fetch(f"{BASE}/ajaxsuche", method="POST", data=form)
        if not isinstance(r2, Response) or r2.status != 200:
            info["error"] = f"load-more stopped at {position}"
            break
        try:
            payload = json.loads(r2.text)
        except json.JSONDecodeError:
            info["error"] = f"load-more not JSON at {position}"
            break
        more = parse_html(payload.get("html", ""))
        if not more:
            break
        entries += more
        position += payload.get("anzahlTreffer") or len(more)
    info["parsed"] = len(entries)
    return entries, info
