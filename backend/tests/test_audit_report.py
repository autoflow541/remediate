"""Tests for audit_report.py -- Sprint 27."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.audit_report import generate_report

CONF = {
    "lang": "en",
    "title": "Test Document",
    "structElements": 42,
    "failures": [],
    "headingIssues": [],
    "linkIssues": [],
    "altIssues": [],
    "contrastFailures": [],
    "contrastRepairs": 2,
    "fontsEmbedded": 1,
    "aiAltGenerated": 3,
    "captionsLinked": 2,
    "formulasTagged": 1,
    "ocgIssues": [],
    "security": {"severity": "ok", "description": "Not encrypted."},
    "readingLevel": {"fleschKincaid": 8.5},
}


class TestGenerateReport:
    def test_returns_string(self):
        html = generate_report("test.pdf", CONF)
        assert isinstance(html, str)

    def test_is_html(self):
        html = generate_report("test.pdf", CONF)
        assert "<html" in html.lower() or "<!doctype" in html.lower()

    def test_contains_filename(self):
        html = generate_report("my_report.pdf", CONF)
        assert "my_report.pdf" in html

    def test_empty_conformance_does_not_crash(self):
        html = generate_report("x.pdf", {})
        assert isinstance(html, str) and len(html) > 100

    def test_verapdf_failures_appear_in_output(self):
        conf = dict(CONF, failures=[
            {"clause": "6-8-1", "description": "Missing /Alt on Figure"},
        ])
        html = generate_report("test.pdf", conf)
        assert "6-8-1" in html or "Missing" in html

    def test_security_error_highlighted(self):
        conf = dict(CONF, security={
            "severity": "error", "encrypted": True,
            "accessibility_allowed": False,
            "description": "Accessibility bit not set.",
        })
        html = generate_report("secure.pdf", conf)
        assert "accessibility" in html.lower() or "encrypt" in html.lower()

    def test_contrast_repairs_shown(self):
        assert "2" in generate_report("test.pdf", CONF)

    def test_ai_alt_count_shown(self):
        assert "3" in generate_report("test.pdf", CONF)
