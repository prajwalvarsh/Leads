from pipeline.crawl import find_link, html_text, links, looks_like_impressum

HOME = """<html><body><nav>
<a href="/">Start</a><a href="/ueber-uns/">Wir</a><a href="/leistungen">Unser Angebot</a>
<a href="https://other.de/impressum">Impressum Partner</a>
<a href="/rechtliches/impressum.html">Impressum</a><a href="mailto:a@b.de">Mail</a>
</nav><script>var x = 1;</script><p>Hausverwaltung Muster</p></body></html>"""


def test_links_resolve_relative_and_skip_mailto() -> None:
    found = links(HOME, "https://www.hv-muster.de/start")
    urls = [u for u, _ in found]
    assert "https://www.hv-muster.de/rechtliches/impressum.html" in urls
    assert not any(u.startswith("mailto") for u in urls)


def test_find_impressum_same_domain_only() -> None:
    found = links(HOME, "https://www.hv-muster.de/")
    assert find_link(found, ["impressum"], "hv-muster.de") == "https://www.hv-muster.de/rechtliches/impressum.html"


def test_find_about_and_services_by_path() -> None:
    found = links(HOME, "https://www.hv-muster.de/")
    assert find_link(found, ["über uns", "ueber-uns"], "hv-muster.de") == "https://www.hv-muster.de/ueber-uns/"
    assert find_link(found, ["leistungen"], "hv-muster.de") == "https://www.hv-muster.de/leistungen"


def test_html_text_falls_back_to_body_text() -> None:
    html = "<html><body><div>Hausverwaltung Muster GmbH</div><div>Musterstraße 1</div>" \
           "<div>10115 Berlin</div><script>ignored()</script></body></html>"
    text = html_text(html, None, 200)
    assert "10115 Berlin" in text and "ignored" not in text


def test_impressum_markers() -> None:
    assert looks_like_impressum("Angaben gemäß § 5 DDG")
    assert looks_like_impressum("IMPRESSUM")
    assert not looks_like_impressum("Willkommen bei uns")
