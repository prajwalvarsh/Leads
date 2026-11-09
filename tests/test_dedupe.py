from pipeline.dedupe import Member, build_company, cluster, primary_key, verdict


def m(cid, source="gelbeseiten", name="X", reg=None, domain=None, phone=None, street=None, **kw) -> Member:
    from pipeline.normalize import normalise_name
    return Member(cid=cid, source=source, name=None if source == "serper_maps" else name,
                  match_name=normalise_name(name), street=None, postcode=kw.get("postcode"), city=kw.get("city"),
                  phone=phone, match_phone=phone, match_street=street, domain=domain, reg=reg,
                  facts=kw.get("facts", {}), status=kw.get("status", "keep"), decided_by=kw.get("by", "llm+rules"),
                  reason=kw.get("reason"))


def ids(groups) -> list[list[str]]:
    return sorted(sorted(x.cid for x in g) for g in groups)


def test_merge_rules() -> None:
    ms = [
        m("a", reg="HRB123B"), m("b", reg="HRB123B", domain="x.de"),     # register
        m("c", domain="x.de"),                                           # domain -> joins a/b
        m("d", phone="+4930111"), m("e", phone="+4930111"),              # phone
        m("f", name="Hausverwaltung Kurz", street=("hauptstrasse", "5")),
        m("g", name="Kurz Hausverwaltung GmbH", street=("hauptstrasse", "5")),   # name + street + number
        m("h", name="Kurz Hausverwaltung", street=("hauptstrasse", "7")),        # other house number
    ]
    assert ids(cluster(ms, 90, 3)[0]) == [["a", "b", "c"], ["d", "e"], ["f", "g"], ["h"]]


def test_never_merge_different_register_numbers() -> None:
    ms = [m("a", reg="HRB1B", phone="+4930111"), m("b", reg="HRB2B", phone="+4930111")]
    groups, merges = cluster(ms, 90, 3)
    assert ids(groups) == [["a"], ["b"]] and merges["blocked_different_register"] == 1
    # nor through a third record without a register number
    ms = [m("a", reg="HRB1B", phone="+4930111"), m("c", phone="+4930111", domain="z.de"), m("b", reg="HRB2B", domain="z.de")]
    assert len(cluster(ms, 90, 3)[0]) == 2


def test_primary_key_order() -> None:
    assert primary_key([m("a", reg="HRB1B", domain="x.de")]) == "reg:HRB1B"
    assert primary_key([m("a", domain="x.de", phone="+4930111")]) == "domain:x.de"
    assert primary_key([m("a", phone="+4930111")]) == "phone:+4930111"


def test_verdict_uses_best_evidence() -> None:
    ms = [m("a", status="keep", by="llm+rules"), m("b", status="review", by="rules", reason="no website and no rule match")]
    assert verdict(ms)[0] == "keep"
    assert verdict([m("a", status="drop", by="llm", reason="not a property manager (llm)")])[0] == "drop"


def test_google_only_cluster_is_excluded_without_name() -> None:
    row = build_company([m("s", source="serper_maps", name="HV Google")], {"s": "cid:1"}, {})
    assert row["tab"] == "excluded" and row["company_name"] is None and row["google_place_id"] == "cid:1"


def test_impressum_facts_win_over_directory() -> None:
    facts = {"company_name": ("Muster HV GmbH", "https://x.de/impressum"), "postcode": ("10117", "u"),
             "street": ("Friedrichstraße 1", "u"), "city": ("Berlin", "u"), "phone": ("+4930111", "u")}
    a = m("a", name="Muster Hausverwaltung", domain="x.de", facts=facts)
    a.street = "Falsche Str. 9"
    row = build_company([a], {}, {"x.de": ("https://www.x.de/", "https://www.x.de/impressum")})
    assert row["company_name"] == "Muster HV GmbH" and row["street"] == "Friedrichstraße 1"
    assert row["status"] == "confirmed" and row["legal_form"] == "GmbH"
