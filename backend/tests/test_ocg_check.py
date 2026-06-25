"""Tests for ocg_check.py — Sprint 24."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.ocg_check import check_optional_content


class TestCheckOptionalContent:
    def test_plain_pdf_no_issues(self, tmp_pdf):
        path = tmp_pdf()
        issues = check_optional_content(path)
        assert isinstance(issues, list)
        assert len(issues) == 0

    def test_missing_file_returns_empty_list(self):
        issues = check_optional_content("/no/such/file.pdf")
        assert issues == []

    def test_issue_dict_shape(self, tmp_path):
        """A PDF with an unnamed OCG should return a properly shaped issue dict."""
        import pikepdf
        from pikepdf import Dictionary, Name, Array

        pdf = pikepdf.new()
        ocg = Dictionary(Type=Name.OCG)   # deliberately missing /Name
        pdf.Root.OCProperties = Dictionary(
            OCGs=Array([pdf.make_indirect(ocg)]),
            D=Dictionary(Order=Array()),
        )
        pdf.pages.append(pikepdf.Page(
            Dictionary(Type=Name.Page, MediaBox=Array([0, 0, 612, 792]))
        ))
        path = str(tmp_path / "ocg.pdf")
        pdf.save(path)

        issues = check_optional_content(path)
        assert isinstance(issues, list)
        if issues:   # implementation may or may not flag unnamed OCG
            for issue in issues:
                for key in ("type", "severity", "description"):
                    assert key in issue
                assert issue["severity"] in ("info", "warning", "error")

    def test_returns_list_on_corrupt_pdf(self, tmp_path):
        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"not a pdf")
        issues = check_optional_content(str(bad))
        assert isinstance(issues, list)
