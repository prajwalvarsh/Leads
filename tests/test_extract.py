from pipeline.extract import canonical, evidence_ok, merge, norm, regex_extract, same

TEXT = """Impressum
Angaben gemäß § 5 DDG
Muster Hausverwaltung GmbH
Friedrichstraße 95
10117 Berlin
Telefon: 030 1234567
Fax: 030 1234568
Registergericht: Amtsgericht Charlottenburg
Registernummer: HRB 123456 B
Webdesign: Agentur Pixel GmbH, Hauptstr. 1, 80331 München"""


def v(value, quote):
    return {"value": value, "quote": quote}


def test_evidence_check() -> None:
    t = norm(TEXT)
    assert evidence_ok("10117", "10117 Berlin", t)
    assert not evidence_ok("10117", "10117  Hamburg", t)          # quote not in text
    assert not evidence_ok("10115", "10117 Berlin", t)            # value not in quote
    assert evidence_ok("Friedrichstraße 95", "Friedrichstraße\n95", t)   # whitespace-normalised
    assert not evidence_ok(None, None, t)


def test_regex_finds_company_block_not_agency() -> None:
    rx = regex_extract(TEXT)
    assert rx["postcode"][0] == "10117" and rx["city"][0] == "Berlin"
    assert canonical("phone", rx["phone"][0]) == "+49301234567"
    assert canonical("register_number", rx["register_number"][0]) == "HRB 123456 B"
    assert rx["register_court"][0] == "Charlottenburg"
    assert rx["legal_form"][0] == "GmbH"


def test_comparison_rules() -> None:
    assert same("register_court", "Amtsgericht Charlottenburg", "Charlottenburg")
    assert same("phone", "030 1234567", "+49 30 1234567")
    assert same("register_number", "HRB 123456 B", "HRB123456B")
    assert not same("postcode", "10117", "10115")


def test_merge_agree_disagree_and_invalid() -> None:
    llm = {
        "company_name": v("Muster Hausverwaltung GmbH", "Muster Hausverwaltung GmbH"),
        "legal_form": v("GmbH", "Muster Hausverwaltung GmbH"),
        "street": v("Friedrichstraße 95", "Friedrichstraße 95"),
        "postcode": v("80331", "Hauptstr. 1, 80331 München"),       # agency address: disagrees with regex
        "city": v("Berlin", "10117 Berlin"),
        "phone": v("030 9999999", "Telefon: 030 9999999"),          # not in the text: rejected
        "register_court": v("Amtsgericht Charlottenburg", "Registergericht: Amtsgericht Charlottenburg"),
        "register_number": v("HRB 123456 B", "Registernummer: HRB 123456 B"),
    }
    kept, reasons, _ = merge(llm, regex_extract(TEXT), norm(TEXT))
    assert kept["register_number"].method == "llm+regex" and kept["register_number"].value == "HRB 123456 B"
    assert kept["postcode"].value == "80331" and "regex disagrees on postcode" in reasons
    assert kept["phone"].method == "regex" and kept["phone"].value == "+49301234567"
    assert kept["company_name"].method == "llm"
