from types import SimpleNamespace

import duckdb

from pipeline.config import cfg
from pipeline.confirm import flag_reasons, listed_as_manager, run_confirm, website_status
from pipeline.db import CONFIRMED_LISTING, CONFIRMED_WEBSITE, REVIEW, SCHEMA

CFG = cfg()["confirm"]


def test_field_flags_and_housing_do_not_block() -> None:
    parts = flag_reasons("housing company; regex disagrees on register_number; regex disagrees on phone")
    assert parts == ["housing company", "register number uncertain", "phone uncertain"]
    assert website_status(parts, CFG["website_flags"]) == CONFIRMED_WEBSITE
    assert website_status(flag_reasons("unclear whether property manager; regex disagrees on phone"),
                          CFG["website_flags"]) == REVIEW
    for r in ("no Impressum found", "not geocoded", "no address", "members disagree", "llm says no, rules say keep"):
        assert website_status(flag_reasons(r), CFG["website_flags"]) == REVIEW


def test_listing_needs_category_and_name_word() -> None:
    cat = [("dasoertliche", "Haus- und Grundstücksverwaltungen")]
    assert listed_as_manager("SAS Liegenschaftsverwaltung GmbH", cat, CFG)
    assert not listed_as_manager("ABG Grundbesitz GmbH", cat, CFG)                      # no keyword
    assert not listed_as_manager("WEG Stargarder Str. 56", cat, CFG)                    # owner association
    assert not listed_as_manager("Müller Hausverwaltung", [("dasoertliche", "Immobilien")], CFG)
    assert not listed_as_manager("Müller Hausverwaltung", [("serper_maps", None)], CFG)  # Google is not a listing
    assert listed_as_manager("Kielmann Klaus HausVerw.", [("11880", "Hausverwaltung & Makler")], CFG)


def test_run_confirm_is_idempotent() -> None:
    con = duckdb.connect()
    con.execute(SCHEMA)
    co = "INSERT INTO company (company_id, company_name, website, bezirk, status, review_reason, decided_by, tab) VALUES (?, ?, ?, ?, ?, ?, ?, 'companies')"
    con.execute(co, ["a", "A GmbH", "https://a.de", "Mitte", "confirmed", None, "llm"])
    con.execute(co, ["b", "B GmbH", "https://b.de", "Mitte", "review", "regex disagrees on phone", "llm"])
    con.execute(co, ["c", "C GmbH", "https://c.de", "Mitte", "review", "unclear whether property manager", "llm"])
    con.execute(co, ["d", "D Hausverwaltung", None, "Mitte", "review", "no Impressum found", "rules"])
    con.execute(co, ["e", "E Hausverwaltung", None, None, "review", "no Impressum found; not geocoded", "rules"])
    for cid in "de":
        con.execute("INSERT INTO candidate (candidate_id, source, source_ref, raw_category) VALUES (?, 'dasoertliche', ?, ?)",
                    [cid + "1", cid, "Haus- und Grundstücksverwaltungen"])
        con.execute("INSERT INTO company_member VALUES (?, ?)", [cid, cid + "1"])
    ctx = SimpleNamespace(con=con, cfg={"confirm": CFG}, run_id="r1", log=lambda *a: None)
    for _ in range(2):
        res = run_confirm(ctx, {})
        got = dict((r[0], r[1:]) for r in con.execute("SELECT company_id, status, review_reason, decided_by FROM company").fetchall())
        assert got == {"a": (CONFIRMED_WEBSITE, None, "llm"), "b": (CONFIRMED_WEBSITE, "phone uncertain", "llm"),
                       "c": (REVIEW, "unclear whether property manager", "llm"),
                       "d": (CONFIRMED_LISTING, "no Impressum found", "listing"),
                       "e": (REVIEW, "no Impressum found; not geocoded", "rules")}
    assert res.counts["newly_confirmed_website"] == 0      # second run: nothing new

    # manual overrides win: name fix, and a move back to review
    ctx.cfg = {"confirm": CFG | {"overrides": {"a": {"company_name": "A Verwaltungs GmbH"},
                                               "b": {"status": "review", "review_reason": "manages only its own property"}}}}
    run_confirm(ctx, {})
    got = dict((r[0], r[1:]) for r in con.execute("SELECT company_id, company_name, status, review_reason, decided_by FROM company").fetchall())
    assert got["a"] == ("A Verwaltungs GmbH", CONFIRMED_WEBSITE, None, "llm")
    assert got["b"] == ("B GmbH", REVIEW, "manages only its own property", "manual")

    # merge_into: duplicate row goes, its members and sources move to the target; a rerun is a no-op
    con.execute("UPDATE company SET sources = 'serper_maps' WHERE company_id = 'a'")
    con.execute("UPDATE company SET sources = 'dasoertliche' WHERE company_id = 'e'")
    ctx.cfg = {"confirm": CFG | {"overrides": {"e": {"merge_into": "a"}}}}
    for _ in range(2):
        res = run_confirm(ctx, {})
    assert con.execute("SELECT count(*) FROM company WHERE company_id = 'e'").fetchone()[0] == 0
    assert con.execute("SELECT sources, source_count FROM company WHERE company_id = 'a'").fetchone() == ("dasoertliche;serper_maps", 2)
    assert con.execute("SELECT company_id FROM company_member WHERE candidate_id = 'e1'").fetchone()[0] == "a"
    assert res.counts["merged_duplicates"] == 0
