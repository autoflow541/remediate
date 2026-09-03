"""/validate is the pure, non-modifying audit endpoint — the one an audit
company actually uses to get findings without touching the client's file.
It must return plain-English + WCAG-cited failures (previously that
enrichment only happened on /remediate's response), and must not fabricate
a PDF/UA explanation for a PDF/A validation run (different spec)."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

import app.main as main_module
from app.validate import RuleResult, ValidationResult


def _canned_result(flavour="ua1"):
    return ValidationResult(
        compliant=False,
        flavour="PDF/UA-1" if flavour == "ua1" else "PDF/A-2B",
        passed_rules=10, failed_rules=1,
        passed_checks=50, failed_checks=1,
        failures=[
            RuleResult(
                specification="ISO 14289-1:2014", clause="7.3", test_number=1,
                status="failed", description="Figure has no /Alt",
                passed_checks=0, failed_checks=1, contexts=["/Page[0]/Figure[0]"],
            ),
        ],
        verapdf_version="veraPDF 1.30.2",
    )


def _client():
    return TestClient(main_module.app)


def test_validate_pdf_ua_enriches_failures(monkeypatch):
    monkeypatch.setattr(main_module, "safe_validate_pdf", lambda path, flavour: _canned_result("ua1"))
    resp = _client().post(
        "/validate",
        files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        data={"flavour": "ua1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    f = body["failures"][0]
    assert f["plain_title"] == "Figure missing alternative text"
    assert f["plain_wcag"] == "1.1.1 Non-text Content (A)"
    assert f["description"] == "Figure has no /Alt"  # original field untouched


def test_validate_pdf_a_is_not_enriched_with_pdf_ua_explanations(monkeypatch):
    """PDF/A clause numbers overlap PDF/UA's numbering scheme but mean
    something else entirely -- must not attach a PDF/UA-specific title/WCAG
    citation to a PDF/A validation run."""
    monkeypatch.setattr(main_module, "safe_validate_pdf", lambda path, flavour: _canned_result("2b"))
    resp = _client().post(
        "/validate",
        files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        data={"flavour": "2b"},
    )
    assert resp.status_code == 200
    f = resp.json()["failures"][0]
    assert "plain_title" not in f
    assert "plain_wcag" not in f


def test_validate_with_no_failures_returns_empty_list(monkeypatch):
    ok_result = ValidationResult(
        compliant=True, flavour="PDF/UA-1", passed_rules=10, failed_rules=0,
        passed_checks=50, failed_checks=0, failures=[],
    )
    monkeypatch.setattr(main_module, "safe_validate_pdf", lambda path, flavour: ok_result)
    resp = _client().post(
        "/validate",
        files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        data={"flavour": "ua1"},
    )
    assert resp.status_code == 200
    assert resp.json()["failures"] == []
