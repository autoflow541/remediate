"""verapdf_explain — plain-English titles/hints + WCAG citations for veraPDF
failures. This is compliance-facing data (audit deliverables cite it), so it's
tested for completeness and for NOT fabricating a citation where none exists."""

from __future__ import annotations

import re

from app.verapdf_explain import _EXPLANATIONS, _FALLBACK, explain_clause, enrich_failures

# Every mapped WCAG citation must look like "<SC number> <name> (<level>)",
# e.g. "1.3.1 Info and Relationships (A)" — catches typos/malformed entries.
_WCAG_FORMAT = re.compile(r"^\d+\.\d+\.\d+ .+ \((A|AA|AAA)\)$")


def test_every_entry_has_title_explanation_hint():
    for clause, exp in _EXPLANATIONS.items():
        assert exp.title.strip(), clause
        assert exp.explanation.strip(), clause
        assert exp.hint.strip(), clause


def test_wcag_field_is_well_formed_or_explicitly_none():
    """No entry may have an empty string or malformed citation -- either a
    real, correctly-formatted SC reference, or an honest None."""
    for clause, exp in _EXPLANATIONS.items():
        if exp.wcag is None:
            continue
        assert _WCAG_FORMAT.match(exp.wcag), f"{clause}: malformed wcag {exp.wcag!r}"


def test_pdf_ua_specific_clauses_are_left_unmapped():
    """These are PDF/UA conformance-metadata/technical requirements with no
    WCAG equivalent (a conformance-claim flag, page geometry consistency,
    encryption permissions, attachment description) -- must NOT get a
    fabricated WCAG citation just to fill the field."""
    for clause in ("7.1-4", "7.1-5", "7.1-6", "7.21-1"):
        assert _EXPLANATIONS[clause].wcag is None


def test_explain_clause_prefers_specific_test_number():
    exp = explain_clause("7.1", test_number=1)
    assert exp is _EXPLANATIONS["7.1-1"]


def test_explain_clause_falls_back_to_bare_clause():
    exp = explain_clause("7.1-1")  # already the full key, no test_number split
    assert exp is _EXPLANATIONS["7.1-1"]


def test_explain_clause_unknown_uses_fallback():
    exp = explain_clause("99.99", test_number=1)
    assert exp is _FALLBACK
    assert exp.wcag is None  # never guess a citation for an unrecognised clause


def test_enrich_failures_adds_wcag_field():
    failures = [{"clause": "7.3", "test_number": 1, "description": "orig"}]
    enriched = enrich_failures(failures)
    assert enriched[0]["plain_wcag"] == "1.1.1 Non-text Content (A)"
    assert enriched[0]["plain_title"] == "Figure missing alternative text"
    assert enriched[0]["description"] == "orig"  # original fields preserved


def test_enrich_failures_unmapped_clause_has_none_wcag():
    failures = [{"clause": "7.1", "test_number": 4}]  # PDF/UA identifier flag
    enriched = enrich_failures(failures)
    assert enriched[0]["plain_wcag"] is None
