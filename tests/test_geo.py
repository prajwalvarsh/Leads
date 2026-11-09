from pipeline.berlin_geo import bezirk_of
from pipeline.geo import clean_street, is_berlin_address


def test_berlin_check_needs_postcode_and_city() -> None:
    assert is_berlin_address("10117", "Berlin")
    assert is_berlin_address("13088", "Berlin-Weißensee")
    assert not is_berlin_address("14482", "Potsdam")
    assert not is_berlin_address("10117", "Hamburg")
    assert not is_berlin_address(None, "Berlin")


def test_point_in_bezirk() -> None:
    assert bezirk_of(52.5208, 13.4095) == "Mitte"                 # Alexanderplatz
    assert bezirk_of(52.5047, 13.3053) == "Charlottenburg-Wilmersdorf"
    assert bezirk_of(52.40, 13.06) is None                        # Potsdam


def test_clean_street() -> None:
    assert clean_street("Geneststraße 5 (Zufahrt Reichartstraße 2, Aufgang G, 4. OG)") == "Geneststraße 5"
    assert clean_street("Marzahner Chaussee 197, Zimmer 737") == "Marzahner Chaussee 197"
