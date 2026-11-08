import duckdb
import pytest

from pipeline.berlin_geo import in_berlin
from pipeline.db import SCHEMA, drop_google_tmp
from pipeline.sources.serper_maps import (
    Place,
    choose_spacing,
    grid_points,
    is_berlin_place,
    pilot_points,
    should_fetch_next,
    store_place,
)


@pytest.mark.parametrize("spacing", [2.0, 3.0, 5.0])
def test_every_grid_point_inside_berlin(spacing: float) -> None:
    pts = grid_points(spacing)
    assert pts
    assert all(in_berlin(p.lat, p.lon) for p in pts)


def test_grid_density_scales() -> None:
    assert len(grid_points(2.0)) > len(grid_points(3.0)) > len(grid_points(5.0))


def test_pilot_points_inside_berlin_and_distinct() -> None:
    pts = pilot_points(5)
    assert len(set(pts)) == 5
    assert all(in_berlin(p.lat, p.lon) for p in pts)


def test_pagination_stop_rule() -> None:
    # (page, results on page, new places on page) -> fetch next?
    assert should_fetch_next(1, 20, 15, max_pages=3, min_new=3)
    assert not should_fetch_next(1, 0, 0, max_pages=3, min_new=3)      # empty page
    assert not should_fetch_next(1, 20, 2, max_pages=3, min_new=3)     # < 3 new
    assert should_fetch_next(2, 20, 3, max_pages=3, min_new=3)         # exactly 3 new
    assert not should_fetch_next(3, 20, 20, max_pages=3, min_new=3)    # max pages reached


def test_berlin_filter() -> None:
    def place(addr: str | None, lat: float | None, lon: float | None) -> Place:
        return Place("x", "n", addr, None, None, lat, lon, None)

    assert is_berlin_place(place("Friedrichstraße 95, 10117 Berlin", None, None))
    assert is_berlin_place(place(None, 52.52, 13.40))                       # coordinates in Mitte
    assert not is_berlin_place(place("Bahnhofstr. 1, 14612 Falkensee", 52.56, 13.08))
    assert not is_berlin_place(place(None, None, None))


def test_choose_spacing_stays_under_cap() -> None:
    spacing, est = choose_spacing([6, 4, 2, 3, 5], cap=1800)
    assert spacing is not None and est[spacing] <= 1800
    smaller = [s for s in est if s < spacing]
    assert all(est[s] > 1800 for s in smaller)
    assert choose_spacing([10_000], cap=1800)[0] is None


def test_serper_tmp_dropped_at_end_of_run() -> None:
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA)
    store_place(con, Place("cid:123", "HV X", "Teststr. 1, 10115 Berlin", "https://x.de", "+4930123",
                           52.53, 13.38, "Hausverwaltung"))
    assert con.execute("SELECT count(*) FROM serper_maps_tmp").fetchone()[0] == 1
    # persistent rows hold only the Google ref
    row = con.execute("SELECT source, source_ref, name, street, phone, website, lat FROM candidate").fetchone()
    assert row == ("serper_maps", "cid:123", None, None, None, None, None)
    assert con.execute("SELECT place_id FROM place_ref").fetchone()[0] == "cid:123"

    drop_google_tmp(con)
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert "serper_maps_tmp" not in tables and "places_tmp" not in tables
