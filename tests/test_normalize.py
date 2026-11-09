from pipeline.normalize import (
    legal_form,
    normalise_name,
    parse_postcode_city,
    parse_register,
    phone_e164,
    registrable_domain,
    split_street,
)


def test_phone_e164() -> None:
    assert phone_e164("030 54 98 98 90") == "+493054989890"
    assert phone_e164("Tel. (030) 31504036") == "+493031504036"
    assert phone_e164("+49 30 2236-7038") == "+493022367038"
    assert phone_e164("030 22 36 ..") is None
    assert phone_e164("") is None


def test_registrable_domain() -> None:
    assert registrable_domain("https://www.axium.de/impressum") == "axium.de"
    assert registrable_domain("http://hv.example.co.uk/x") == "example.co.uk"
    assert registrable_domain("www.hhv.info") == "hhv.info"
    assert registrable_domain("foo.jimdosite.com") == "jimdosite.com"
    assert registrable_domain(None) is None


def test_normalise_name() -> None:
    assert normalise_name("Bonitas Hausverwaltungs- und Immobilien GmbH") == "bonitas hausverwaltungs und immobilien"
    assert normalise_name("Müller & Söhne Hausverwaltung GmbH & Co. KG") == "mueller soehne hausverwaltung"
    assert normalise_name("Hausverwaltung Schulz e.K.") == "hausverwaltung schulz"


def test_legal_form() -> None:
    assert legal_form("Dr. Cramer Treuhand mbH & Co. KG") == "GmbH & Co. KG" or legal_form(
        "Dr. Cramer Treuhand GmbH & Co. KG") == "GmbH & Co. KG"
    assert legal_form("Muster Verwaltung UG (haftungsbeschränkt)") == "UG (haftungsbeschränkt)"
    assert legal_form("ABC Immobilien GmbH") == "GmbH"
    assert legal_form("Wohnungsbaugenossenschaft Beispiel eG") == "eG"
    assert legal_form("Hausverwaltung Schulz e.K.") == "e.K."
    assert legal_form("Hausverwaltung Schulz") is None


def test_parse_register() -> None:
    r = parse_register("Registergericht: Amtsgericht Charlottenburg, HRB 123456 B")
    assert r and r.kind == "HRB" and r.number == "123456" and r.suffix == "B"
    assert r.court == "Charlottenburg"
    assert r.display() == "Amtsgericht Charlottenburg HRB 123456 B"

    r2 = parse_register("Amtsgericht Berlin-Charlottenburg HRB123456B")
    assert r2 and r2.key == r.key and r2.court == "Charlottenburg"

    r3 = parse_register("Handelsregister: HRA 4711, Amtsgericht München")
    assert r3 and r3.kind == "HRA" and r3.suffix == "" and r3.court == "München"

    assert parse_register("Keine Registereintragung") is None

    # the next capitalised word after the court is not part of the court name
    r4 = parse_register("HRB 216358B Registergericht: Berlin Charlottenburg Umsatzsteuer-Identifikationsnummer")
    assert r4 and r4.court == "Charlottenburg"
    r5 = parse_register("HRB 221169 B Registergericht: Amtsgericht Charlottenburg Vertreten durch: X")
    assert r5 and r5.court == "Charlottenburg"
    # formats seen in the extract run
    for text, court in [
        ("Registergericht: AG Berlin - Charlottenburg, HRB 12345", "Charlottenburg"),
        ("Amtsgericht Berlin – Charlottenburg HRB 12345 B", "Charlottenburg"),
        ("Amtsgericht Charlottenburg von Berlin HRB 12345", "Charlottenburg"),
        ("Amtsgericht Charlottenburg Erlaubnis nach § 34c GewO HRB 12345", "Charlottenburg"),
        ("Amtsgericht Frankfurt (Oder) HRB 12345", "Frankfurt (Oder)"),
        ("Amtsgericht Berlin HRB 12345", "Berlin"),
        ("Registergericht: Amtsgericht Charlottenburg Registernummer: HRB 12345", "Charlottenburg"),
    ]:
        r = parse_register(text)
        assert r and r.court == court, (text, r)


def test_address_helpers() -> None:
    assert parse_postcode_city("10117 Berlin, Mitte") == ("10117", "Berlin")
    assert parse_postcode_city("14612 Falkensee") == ("14612", "Falkensee")
    assert split_street("Friedrichstr. 95") == ("friedrichstrasse", "95")
    assert split_street("Friedrichstraße 95a") == ("friedrichstrasse", "95a")
