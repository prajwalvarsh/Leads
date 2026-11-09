from pipeline.overlap import Rec, match_reason, overlap


def rec(key: str, domain=None, phone=None, name="", street=None) -> Rec:
    return Rec(key, domain, phone, name, street)


def test_match_reasons() -> None:
    s = rec("s", "hv-x.de", "+4930111", "hv x", ("friedrichstrasse", "95"))
    assert match_reason(s, rec("o", domain="hv-x.de")) == "domain"
    assert match_reason(s, rec("o", phone="+4930111")) == "phone"
    assert match_reason(s, rec("o", name="hv x", street=("friedrichstrasse", "95"))) == "name_street"
    # same name, different house number -> no match
    assert match_reason(s, rec("o", name="hv x", street=("friedrichstrasse", "12"))) is None
    # same street, different name -> no match
    assert match_reason(s, rec("o", name="ganz andere verwaltung", street=("friedrichstrasse", "95"))) is None


def test_overlap_counts() -> None:
    serper = [rec("s1", "a.de"), rec("s2", phone="+4930222"), rec("s3", "c.de")]
    other = [rec("o1", "a.de"), rec("o2", phone="+4930222"), rec("o3", "z.de")]
    o = overlap(serper, other)
    assert (o["serper_matched"], o["other_matched"], o["serper_only"], o["other_only"]) == (2, 2, 1, 1)
    assert o["matched_by"] == {"domain": 1, "phone": 1, "name_street": 0}
