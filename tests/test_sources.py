from pathlib import Path

from pipeline.sources import dasoertliche, elf880, gelbeseiten, osm
from pipeline.sources.places import Tile, initial_tiles
from pipeline.sources.search import classify_url

FIX = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_gelbeseiten_parser() -> None:
    rows = gelbeseiten.parse_html(read("gelbeseiten_mitte.html"))
    assert len(rows) == 35
    first = rows[0]
    assert first.name == "AXIUM - Hausverwaltung - Property Management Berlin"
    assert first.street == "Unter Den Linden 10"
    assert (first.postcode, first.city) == ("10117", "Berlin")
    assert first.phone == "+493054989890"
    assert first.website == "https://www.axium.de/"
    assert first.source_ref.startswith("https://www.gelbeseiten.de/gsbiz/")
    assert all("(" not in (r.street or "") for r in rows)


def test_dasoertliche_parser() -> None:
    rows, nxt = dasoertliche.parse_html(read("dasoertliche_mitte.html"))
    assert len(rows) == 25
    assert nxt and "recFrom=26" in nxt
    r = rows[0]
    assert r.name == "AG für Haus- und Grundbesitz"
    assert (r.street, r.postcode, r.city) == ("Wallstr. 15", "10179", "Berlin")
    assert "hitno" not in r.source_ref and "id=" in r.source_ref


def test_dasoertliche_locations() -> None:
    assert dasoertliche.locations("Mitte") == ["Berlin-Mitte"]
    assert dasoertliche.locations("Charlottenburg-Wilmersdorf") == [
        "Berlin-Charlottenburg", "Berlin-Wilmersdorf"]


def test_11880_parser() -> None:
    rows, has_next = elf880.parse_html(read("11880_mitte.html"), 1)
    assert len(rows) == 50 and has_next
    r = rows[0]
    assert (r.street, r.postcode, r.city) == ("Friedrichstr. 95", "10117", "Berlin")
    assert r.phone == "+493031504036"


def test_osm_parse() -> None:
    els = [
        {"type": "node", "id": 1, "lat": 52.5, "lon": 13.4,
         "tags": {"name": "Hausverwaltung X", "office": "property_management",
                  "addr:street": "Teststr.", "addr:housenumber": "1", "addr:postcode": "10115",
                  "addr:city": "Berlin", "phone": "+49 30 1234567", "website": "https://x.de"}},
        {"type": "way", "id": 2, "center": {"lat": 52.4, "lon": 13.3},
         "tags": {"name": "Immobilienverwaltung Y"}},
        {"type": "node", "id": 3, "tags": {"office": "property_management"}},  # no name
    ]
    rows = osm.parse(els)
    assert len(rows) == 2
    assert rows[0].street == "Teststr. 1" and rows[0].phone == "+49301234567"
    assert rows[0].raw_category == "office=property_management"
    assert rows[1].source_ref == "way/2" and rows[1].lat == 52.4


def test_places_grid() -> None:
    tiles = initial_tiles(4)
    assert len(tiles) == 16
    t = Tile(52.0, 13.0, 52.01, 13.01)
    kids = t.split()
    assert len(kids) == 4 and all(k.depth == 1 for k in kids)
    assert 600 < t.edge_m() < 1200


def test_classify_url() -> None:
    assert classify_url("https://www.gelbeseiten.de/branchen/x") == "portal"
    assert classify_url("https://hv-muster.jimdosite.com/") == "shared"
    assert classify_url("https://www.axium.de/") == "company"
