"""Normalisation helpers: phone, domain, names, register numbers, legal forms, addresses."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import phonenumbers
import tldextract

# Bundled public-suffix snapshot, no network fetch.
_tld = tldextract.TLDExtract(suffix_list_urls=())


# ---------- phone ----------
def phone_e164(raw: str | None, region: str = "DE") -> str | None:
    if not raw:
        return None
    raw = raw.replace("\xa0", " ").strip()
    if ".." in raw or "…" in raw or "*" in raw:  # truncated/masked numbers in directories
        return None
    raw = re.sub(r"^(tel\.?|telefon|fon|phone)[:\s]*", "", raw, flags=re.I)
    try:
        num = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


# ---------- domain ----------
def registrable_domain(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if "://" not in url:
        url = "http://" + url
    ext = _tld(url)
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}".lower()


def is_listed_domain(domain: str | None, listed: list[str]) -> bool:
    """True if domain equals or is a subdomain of an entry in the list."""
    if not domain:
        return False
    return any(domain == d or domain.endswith("." + d) for d in listed)


def site_root(url: str) -> str:
    """https://www.example.de/foo?x -> https://www.example.de/"""
    m = re.match(r"^(https?://[^/?#]+)", url.strip(), flags=re.I)
    return (m.group(1) if m else url).rstrip("/") + "/"


# ---------- text ----------
_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def fold(text: str) -> str:
    """Lowercase, umlauts -> ae/oe/ue/ss, strip other accents."""
    text = text.lower().translate(_UMLAUT)
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def clean_ws(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


# ---------- legal form ----------
# Order matters: longest/most specific first.
LEGAL_FORMS: list[tuple[str, str]] = [
    ("GmbH & Co. KG", r"gmbh\s*(&|und)\s*co\.?\s*kg"),
    ("UG (haftungsbeschränkt)", r"\bug\s*\(?\s*haftungsbeschr(ä|ae)nkt\s*\)?|\bug\b"),
    ("GmbH", r"\bgmbh\b|gesellschaft mit beschr(ä|ae)nkter haftung|\bmbh\b"),
    ("AG", r"\bag\b|aktiengesellschaft"),
    ("KG", r"\bkg\b|kommanditgesellschaft"),
    ("OHG", r"\bohg\b|offene handelsgesellschaft"),
    ("GbR", r"\bgbr\b|gesellschaft b(ü|ue)rgerlichen rechts"),
    ("e.K.", r"\be\.\s?k(fm|ffr)?\.?(?=\s|$|,)|eingetragene[rn]? kauf(mann|frau)"),
    ("eG", r"\beg\b|eingetragene genossenschaft"),
]


def legal_form(name: str | None) -> str | None:
    if not name:
        return None
    low = name.lower()
    for label, pat in LEGAL_FORMS:
        if re.search(pat, low):
            return label
    return None


_LEGAL_STRIP = re.compile(
    r"gmbh\s*(&|und)\s*co\.?\s*kg|ug\s*\(haftungsbeschr(ae|a)nkt\)|\b(gmbh|mbh|ug|ag|kg|ohg|gbr|e\.?\s?k\.?|eg|"
    r"inh\.?|inhaber(in)?)\b"
)


def normalise_name(name: str | None) -> str:
    """For fuzzy matching: folded, legal forms and punctuation removed."""
    if not name:
        return ""
    n = fold(name)
    n = _LEGAL_STRIP.sub(" ", n)
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


# ---------- register ----------
@dataclass(frozen=True)
class Register:
    court: str | None  # e.g. "Charlottenburg"
    kind: str          # HRA / HRB / GnR / PR / VR
    number: str        # digits
    suffix: str        # e.g. "B" or ""

    @property
    def key(self) -> str:
        """Normalised key for dedupe (court ignored: often missing/variant spelled)."""
        return f"{self.kind}{self.number}{self.suffix}"

    def display(self) -> str:
        court = f"Amtsgericht {self.court} " if self.court else ""
        return f"{court}{self.kind} {self.number}{(' ' + self.suffix) if self.suffix else ''}"


_REG = re.compile(
    r"\b(HR\s?[AB]|GnR|PR|VR)\s*[:.\-]?\s*(?:Nr\.?\s*)?(\d{2,6})(?:\s*([A-Z]))?\b"
)
_COURT = re.compile(
    r"(?:Amtsgericht|AG|Registergericht)[:\s]+(?:Berlin[\s\-]*)?\(?((?!HR|GnR|PR\b|VR\b|Amtsgericht)[A-ZÄÖÜ][\wäöüß\-]+"
    r"(?:\s(?!HR|GnR|PR\b|VR\b|Nr|Register|Handels|Steuer|USt|Sitz|Geschäfts)[A-ZÄÖÜ][\wäöüß\-]+)?)\)?"
)


def parse_register(text: str | None) -> Register | None:
    if not text:
        return None
    m = _REG.search(text)
    if not m:
        return None
    kind = m.group(1).replace(" ", "").upper().replace("GNR", "GnR")
    number = m.group(2)
    suffix = (m.group(3) or "").upper()
    # Only the Berlin "B" suffix is part of the number; other trailing capitals are words.
    if suffix and suffix != "B":
        suffix = ""
    window = text[max(0, m.start() - 120): m.end() + 120]
    c = _COURT.search(window)
    court = c.group(1).strip() if c else None
    return Register(court, kind, number, suffix)


# ---------- address ----------
_PLZ_CITY = re.compile(r"\b(\d{5})\s+([A-ZÄÖÜ][\wäöüß.\- ]*?)(?:\s*[,(]|$)")


def parse_postcode_city(line: str | None) -> tuple[str | None, str | None]:
    """'10117 Berlin, Mitte' -> ('10117', 'Berlin')."""
    if not line:
        return None, None
    m = _PLZ_CITY.search(clean_ws(line))
    if not m:
        return None, None
    return m.group(1), m.group(2).strip()


_STREET = re.compile(r"^(.*?)[\s,]*(\d+\s*[a-zA-Z]?(?:\s*[-/]\s*\d+\s*[a-zA-Z]?)?)\s*$")


def split_street(street: str | None) -> tuple[str, str]:
    """'Friedrichstr. 95' -> ('friedrichstrasse', '95'). Used for dedupe."""
    s = fold(clean_ws(street))
    s = re.sub(r"str\.|str\b", "strasse", s)
    m = _STREET.match(s)
    if not m:
        return re.sub(r"[^a-z0-9]", "", s), ""
    name = re.sub(r"[^a-z0-9]", "", m.group(1))
    num = re.sub(r"\s+", "", m.group(2))
    return name, num
