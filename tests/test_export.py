import duckdb

from pipeline.db import SCHEMA
from pipeline.export import (DETAILS_HEADER, LEADS_HEADER, NO_WEBSITE, IN_REVIEW, kpi_definitions,
                             leads_format_requests, leads_tables, why_text)


def db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(SCHEMA)
    co = "INSERT INTO company (company_id, company_name, street, postcode, city, website, status, review_reason, tab) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    con.execute(co, ["a", "Alpha HV GmbH", "Hauptstr. 1", "10115", "Berlin", "https://alpha.de", "confirmed", None, "companies"])
    con.execute(co, ["b", "Beta HV", None, None, None, None, "review", "no Impressum found", "companies"])
    con.execute(co, ["c", "Gamma HV", None, None, None, "https://gamma.de", "review", "unclear whether property manager", "companies"])
    con.execute(co, ["d", "Delta HV", None, None, None, "https://delta.de", "confirmed", None, "companies"])
    con.execute(co, ["x", "Excluded Ltd", None, None, None, None, None, None, "excluded"])
    con.execute("""INSERT INTO kpi (company_id, units_managed, units_managed_text, segments, metering_provider,
                   remote_reading_mentioned, digital_tools, decision_maker, decision_maker_role, personal_email, email_basis)
                   VALUES ('a', 900, 'über 900', 'WEG, Miet', 'not named', 'no', 'yes', 'Anna Weber', 'Geschäftsführerin',
                           'a.weber@alpha.de', 'published'),
                          ('d', NULL, NULL, 'SE', 'Techem', 'yes', 'no', 'Max Muster', NULL, NULL, NULL)""")
    con.execute("""INSERT INTO priority VALUES
                   ('a', 'High', 'WEG, Miet, 900 units, no remote reading mentioned, no metering provider named'),
                   ('d', 'Low', 'no WEG or Miet segment found (SE only)')""")
    con.execute("INSERT INTO linkedin VALUES ('a', 'Anna Weber', 'q', 'https://linkedin.com/in/aw', 't', 'r')")
    return con


def test_leads_columns_order_and_labels() -> None:
    leads, details = leads_tables(db())
    assert leads[0] == LEADS_HEADER and len(LEADS_HEADER) == 11
    assert [r[0] for r in leads[1:]] == ["Alpha HV GmbH", "Delta HV", "Gamma HV", "Beta HV"]   # High, Low, in review, no website
    assert [r[1] for r in leads[1:]] == ["High", "Low", IN_REVIEW, NO_WEBSITE]
    alpha = dict(zip(LEADS_HEADER, leads[1]))
    assert alpha["Contact person"] == "Anna Weber (Geschäftsführerin)" and alpha["Email"] == "a.weber@alpha.de"
    assert alpha["LinkedIn"] == "https://linkedin.com/in/aw" and alpha["Units managed"] == 900
    assert alpha["Why"].endswith("; uses digital tools (portal/software)")
    assert leads[2][2] == "no WEG or Miet segment found (SE only); metering provider Techem, remote reading mentioned"
    assert leads[4][2] == "no Impressum found"            # not checked: Why = review reason
    assert details[0] == DETAILS_HEADER and details[0][:2] == ["Company", "ID"]
    a = dict(zip(DETAILS_HEADER, details[1]))
    assert a["Address"] == "Hauptstr. 1, 10115 Berlin" and a["Email source"] == "published" and a["Units basis"] == "über 900"
    assert len(details) == 5                               # excluded companies are not in Details


def test_why_does_not_repeat_what_the_rule_says() -> None:
    why = "WEG, 900 units, remote reading mentioned and provider Techem"
    assert why_text(why, None, "Techem", "yes", "no") == why


def test_kpi_definitions_report_fill_rates() -> None:
    text = str(kpi_definitions(db()))
    assert "found for 1 of 2 confirmed companies (50%)" in text and "is_housing_company" in text


def test_leads_format_replaces_old_rules() -> None:
    reqs = leads_format_requests(7, 10, old_rules=5)
    assert sum("deleteConditionalFormatRule" in r for r in reqs) == 5
    assert sum("addConditionalFormatRule" in r for r in reqs) == 4
