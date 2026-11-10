from pipeline.linkedin import clean_company, clean_person, pick
from pipeline.priority import decide


def d(segments, units, remote="no", provider="not named", housing="no"):
    return decide(segments, units, remote, provider, housing, low_below=30, high_min=200)


def test_low_rules() -> None:
    assert d(None, 5000)[0] == "Low"                    # no segment found
    assert d("SE, Gewerbe", 5000)[0] == "Low"           # no WEG or Miet
    assert d("WEG", 20)[0] == "Low"                     # stated below 30


def test_high_rule() -> None:
    p, why = d("WEG, Miet", 1200)
    assert p == "High" and why == "WEG, Miet, 1,200 units, no remote reading mentioned, no metering provider named"
    assert d("WEG", 200, remote="yes", provider="not named")[0] == "High"     # open: no provider named
    assert d("Miet", 500, remote="no", provider="Techem")[0] == "High"        # open: no remote reading mentioned


def test_medium_rules() -> None:
    assert d("WEG", None) == ("Medium", "WEG, units unknown")                  # unknown units -> Medium
    assert d("WEG", 150)[0] == "Medium"                                         # below the High threshold
    assert d("WEG", 900, remote="yes", provider="Techem")[0] == "Medium"       # locked in


def test_housing_company_keeps_priority() -> None:
    p, why = d("Miet", 9000, housing="yes")
    assert p == "High" and why.endswith("housing company: buys via tenders")


def test_linkedin_match_needs_person_and_company() -> None:
    assert clean_person("Herr Dipl.-Ing. Tobias Seidel") == "Tobias Seidel"
    assert clean_company("Omnia Hausverwaltung GmbH") == "Omnia Hausverwaltung"
    data = {"organic": [
        {"link": "https://de.linkedin.com/in/other", "title": "Tobias Seidel - Siemens", "snippet": ""},
        {"link": "https://www.linkedin.com/company/omnia", "title": "Omnia Hausverwaltung", "snippet": "Tobias Seidel"},
        {"link": "https://de.linkedin.com/in/tobias-seidel-1?trk=x", "title": "Tobias Seidel – Geschäftsführer – Omnia Hausverwaltung GmbH",
         "snippet": ""},
    ]}
    assert pick(data, "Tobias Seidel", "Omnia Hausverwaltung")[0] == "https://de.linkedin.com/in/tobias-seidel-1"
    assert pick({"organic": data["organic"][:2]}, "Tobias Seidel", "Omnia Hausverwaltung") is None
    # the company name contains the person's name: another employee's profile is not a match
    other = {"organic": [{"link": "https://de.linkedin.com/in/ts", "title": "Tobias Schotte - Wilhelm Hilpert Hausverwaltung", "snippet": ""}]}
    assert pick(other, "Wilhelm Hilpert", "Wilhelm Hilpert Hausverwaltung") is None
