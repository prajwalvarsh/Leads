from pipeline.classify import decide, loose, rule_verdict

KEEP = ["hausverwalt", "immobilienverwalt", "weg-verwalt", "mietverwalt"]
DROP = ["makler", "hausmeister", "gebäudereinigung", "kanzlei"]


def test_rules_keep_wins_over_drop() -> None:
    assert rule_verdict(["Müller Hausverwaltung und Hausmeisterservice"], KEEP, DROP) == ("keep", "hausverwalt")
    assert rule_verdict(["Schulz Immobilienmakler"], KEEP, DROP) == ("drop", "makler")
    assert rule_verdict(["Gerhardt Silvia", None], KEEP, DROP) == ("none", None)
    assert rule_verdict(["HAUSVERWALTUNG BERLIN"], KEEP, DROP)[0] == "keep"


def test_decision_matrix() -> None:
    assert decide("yes", "keep").decided_by == "llm+rules"
    assert decide("no", "drop").status == "drop"
    assert decide("yes", "none").decided_by == "llm" and decide("yes", "none").status == "keep"
    assert decide("yes", "drop").status == "review"          # disagreement
    assert decide("no", "keep").status == "review"
    assert decide("unclear", "keep").status == "review"
    assert decide("yes", "keep", housing=True).reason == "housing company"


def test_quote_check_ignores_case_and_dashes_not_numbers() -> None:
    text = "Wir verwalten seit 1995 – mit Herz – rund 7.000 Wohnungen."
    assert loose("wir verwalten seit 1995 - mit herz") in loose(text)
    assert loose("rund 7,000 Wohnungen") not in loose(text)


def test_no_website_rules_only() -> None:
    assert (decide(None, "keep").status, decide(None, "keep").decided_by) == ("keep", "rules")
    assert decide(None, "drop").status == "drop"
    assert decide(None, "none").status == "review"
