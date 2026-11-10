from pipeline.extract import norm
from pipeline.kpi import (build_text, email_for, emails_in, evaluate, infer_pattern, is_role_address,
                          matches_person, name_parts, parse_number)

ROLES = ["info", "kontakt", "office", "mail", "verwaltung", "buchhaltung", "service", "post"]
URL = "https://hv-muster.de/team"
PAGES = {URL: norm("Seit 1992 verwalten wir über 3.500 Wohneinheiten in Berlin. Wir verwalten WEG und Mietshäuser. "
                   "Geschäftsführer: Jürgen Schmidt. Ansprechpartnerin Anna Weber a.weber@hv-muster.de "
                   "Kontakt: info@hv-muster.de")}


def vq(v, q):
    return {"value": v, "quote": q}


def llm_out(**kw):
    out = {"units_managed": vq(None, ""), "segments": [], "is_housing_company": vq("no", ""),
           "metering_provider": vq("not named", ""), "remote_reading_mentioned": vq("no", ""),
           "digital_tools": vq("no", ""), "decision_maker": {"name": None, "role": None, "quote": ""},
           "personal_email": vq(None, "")}
    return out | kw


def test_evidence_rejects_quote_not_on_page() -> None:
    row, _, stats = evaluate(llm_out(units_managed=vq("über 9.000", "wir verwalten über 9.000 Einheiten"),
                                     metering_provider=vq("Techem", "Abrechnung über Techem")), PAGES, "hv-muster.de", ROLES)
    assert row["units_managed"] is None and stats["units_managed"] == "rejected"
    assert row["metering_provider"] == "not named" and stats["metering_provider"] == "rejected"


def test_unit_count_needs_unit_context() -> None:
    from pipeline.kpi import is_unit_quote
    assert is_unit_quote("Wir betreuen über 9.600 Wohn- und Gewerbeeinheiten in Berlin")
    assert not is_unit_quote("verwaltetes Immobilienvermögen deutlich über 1.500.000.000,00 EUR")
    assert not is_unit_quote("weit über 5.000 Wohnungen und Geschäfte saniert")
    assert not is_unit_quote("über 8.000")


def test_evidence_keeps_supported_values() -> None:
    out = llm_out(units_managed=vq("über 3.500", "verwalten wir über 3.500 Wohneinheiten"),
                  segments=[{"segment": "WEG", "quote": "Wir verwalten WEG und Mietshäuser."},
                            {"segment": "Miet", "quote": "Wir verwalten WEG und Mietshäuser."},
                            {"segment": "Gewerbe", "quote": "Wir verwalten WEG und Mietshäuser."}],   # not supported
                  decision_maker={"name": "Jürgen Schmidt", "role": "Geschäftsführer", "quote": "Geschäftsführer: Jürgen Schmidt."})
    row, _, _ = evaluate(out, PAGES, "hv-muster.de", ROLES)
    assert row["units_managed"] == 3500 and row["segments"] == "WEG, Miet"
    assert row["decision_maker"] == "Jürgen Schmidt" and row["decision_maker_role"] == "Geschäftsführer"


def test_role_addresses_never_personal() -> None:
    for e in ["info@x.de", "kontakt@x.de", "office@x.de", "verwaltung@x.de", "buchhaltung@x.de", "info.berlin@x.de", "post@x.de"]:
        assert is_role_address(e, ROLES), e
    for e in ["j.schmidt@x.de", "anna.weber@x.de", "mueller@x.de"]:
        assert not is_role_address(e, ROLES), e
    # an info@ address is never returned as the decision maker's email
    pages = {URL: norm("Geschäftsführer: Jürgen Schmidt. E-Mail: info@hv-muster.de")}
    assert email_for("Jürgen Schmidt", "info@hv-muster.de", pages, "hv-muster.de", ROLES)[0] is None


def test_umlaut_normalisation_and_pattern(monkeypatch) -> None:
    assert name_parts("Dipl.-Kfm. Jürgen Schmidt") == ("juergen", "schmidt")
    assert matches_person("j.schmidt@hv-muster.de", "Jürgen Schmidt")
    assert matches_person("juergen.schmidt@hv-muster.de", "Jürgen Schmidt")
    assert infer_pattern("a.weber@hv-muster.de", ["Anna Weber"]) == "f.last"
    # a relative's address is not the decision maker's
    assert not matches_person("tina.grieben@havika.de", "Björn Grieben")
    assert matches_person("wiechert-hv@mail.de", "Sabine Wiechert")
    assert matches_person("hwaber@hiob-gmbh.de", "Henrik Waber")
    monkeypatch.setattr("pipeline.kpi.has_mx", lambda d: True)
    email, basis, _, _ = email_for("Jürgen Schmidt", None, PAGES, "hv-muster.de", ROLES)
    assert (email, basis) == ("j.schmidt@hv-muster.de", "pattern")


def test_obfuscated_addresses() -> None:
    assert emails_in("j.schmidt(at)hv-muster.de und k.lang [at] hv-muster (dot) de") == {"j.schmidt@hv-muster.de", "k.lang@hv-muster.de"}


def test_numbers_and_text_order() -> None:
    assert parse_number("über 3.500") == 3500 and parse_number("rund 1200") == 1200 and parse_number(None) is None
    t = build_text({"home": ("h", "H" * 9000), "impressum": ("i", "I" * 9000), "jobs": ("j", "J" * 100)}, 12000)
    assert t.index("### impressum") < t.index("### jobs") < t.index("### home") and len(t) < 12200
