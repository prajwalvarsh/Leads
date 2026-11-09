import json
from pathlib import Path

from pipeline.resolve import Group, Item, domain_score, group_items, has_keyword, pick_result, tier

KW = ["Hausverwaltung", "Immobilienverwaltung", "WEG", "Mietverwaltung"]
EXCLUDED = ["facebook.com", "gelbeseiten.de", "meineoeffnungszeiten.de"]


def item(cid, source="gelbeseiten", name="X", domain=None, phone=None, street=None) -> Item:
    from pipeline.normalize import normalise_name
    return Item(cid, source, name, f"https://{domain}/" if domain else None, domain, phone,
                normalise_name(name), street, source == "serper_maps")


def test_domain_score() -> None:
    assert domain_score("Hausverwaltung Müller GmbH", "hausverwaltung-mueller.de") >= 80
    assert domain_score("Hausverwaltung Müller GmbH", "hvmueller.de") >= 80          # no hyphen
    assert domain_score("Müller Immobilienverwaltung", "mueller-hv.de") >= 80
    # generic words alone never match
    assert domain_score("Hausverwaltung Schmidt", "hausverwaltung-berlin.de") < 80
    assert domain_score("Hausverwaltung Schmidt", "weber-immobilien.de") < 80


def test_pick_result_skips_portals_and_mismatches() -> None:
    data = {"organic": [
        {"link": "https://www.gelbeseiten.de/gsbiz/1"},
        {"link": "https://www.weber-immobilien.de/impressum"},
        {"link": "https://www.hv-schmidt.de/impressum"},
    ]}
    assert pick_result("Hausverwaltung Schmidt", data, EXCLUDED, 80)[:2] == ("https://www.hv-schmidt.de/", "hv-schmidt.de")
    assert pick_result("Hausverwaltung Schmidt", {"organic": []}, EXCLUDED, 80) is None


def test_real_search_sample_has_no_company_site() -> None:
    data = json.loads((Path(__file__).parent / "fixtures" / "serper_search_sample.json").read_text("utf-8"))
    assert pick_result("b2 Fläche+Raum Immobilienverwaltung", data, EXCLUDED, 80) is None


def test_grouping_by_phone_and_name_street() -> None:
    items = [
        item("a", "gelbeseiten", "HV Alpha GmbH", domain="hv-alpha.de", phone="+4930111"),
        item("b", "dasoertliche", "HV Alpha", phone="+4930111"),
        item("c", "11880", "Hausverwaltung Beta", street=("friedrichstrasse", "5")),
        item("d", "serper_maps", "Beta Hausverwaltung", street=("friedrichstrasse", "5")),
        item("e", "osm", "Gamma", street=("friedrichstrasse", "5")),
    ]
    groups = sorted(sorted(i.cid for i in g.items) for g in group_items(items, 90))
    assert groups == [["a", "b"], ["c", "d"], ["e"]]


def test_shared_phone_does_not_chain_companies() -> None:
    items = [item(c, name=n, phone="+4930999") for c, n in [("a", "Alpha"), ("b", "Beta"), ("c", "Gamma"), ("d", "Delta")]]
    assert len(group_items(items, 90)) == 4


def test_search_priority() -> None:
    assert tier(Group([item("a", "gelbeseiten"), item("b", "osm")]), KW) == 0
    assert tier(Group([item("a", name="WEG-Verwaltung Kurz")]), KW) == 1
    assert tier(Group([item("a", name="Wegner Immobilien")]), KW) == 2
    assert has_keyword("Bauland GmbH Hausverwaltungen", KW)
    assert not has_keyword("BEKO Grundstücks GmbH", KW)
