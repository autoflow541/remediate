"""fix_link_text — generating descriptive accessible names for PDF links
(WCAG 2.4.4). Pure logic: URL slug, mailto/tel schemes, domain fallback."""

from __future__ import annotations

from app.fix_link_text import generate_link_description, _slug_to_label


def test_empty_url():
    assert generate_link_description("") == "Link"


def test_mailto_describes_the_address():
    assert generate_link_description("mailto:jane@acme.com") == "Email jane@acme.com"


def test_mailto_with_query_keeps_only_the_address():
    # ?subject=... must not leak into the label.
    assert generate_link_description(
        "mailto:jane@acme.com?subject=Hello") == "Email jane@acme.com"


def test_tel_describes_a_call():
    assert generate_link_description("tel:+15551234567") == "Call +15551234567"


def test_descriptive_slug_wins():
    out = generate_link_description("https://www.example.com/reports/annual-report.pdf")
    assert out == "Annual Report — example.com"


def test_acronym_upper_cased_in_slug():
    out = generate_link_description("https://example.gov/guides/wcag-overview.html")
    assert out.startswith("WCAG Overview")


def test_generic_slug_falls_back_to_domain_without_api():
    # "here" is generic and there is no ANTHROPIC_API_KEY in the test env, so
    # the domain fallback is used.
    out = generate_link_description("https://example.com/here")
    assert out == "Resource on example.com"


def test_slug_to_label_generic_returns_none():
    assert _slug_to_label("/click/here") is None
    assert _slug_to_label("/") is None
