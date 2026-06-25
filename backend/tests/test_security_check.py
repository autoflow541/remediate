"""Tests for security_check.py -- Sprint 25."""
import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pikepdf
from app.security_check import check_security


class TestCheckSecurity:
    def test_unencrypted_pdf_returns_ok(self, tmp_pdf):
        result = check_security(tmp_pdf())
        assert result["encrypted"] is False
        assert result["severity"] == "ok"
        assert result["accessibility_allowed"] is True

    def test_result_has_required_keys(self, tmp_pdf):
        result = check_security(tmp_pdf())
        for key in ("encrypted", "accessibility_allowed",
                    "content_copy_allowed", "severity", "description"):
            assert key in result

    def test_missing_file_returns_dict(self):
        result = check_security("/nonexistent/file.pdf")
        assert isinstance(result, dict)
        assert "severity" in result

    def test_severity_values_are_valid(self, tmp_pdf):
        result = check_security(tmp_pdf())
        assert result["severity"] in ("ok", "warning", "error")

    def test_encrypted_copy_restricted_is_warning(self, tmp_path):
        """PDF spec mandates accessibility=True for AES-128+; extract=False is testable."""
        pdf = pikepdf.new()
        pdf.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name.Page, MediaBox=pikepdf.Array([0,0,612,792]))))
        path = str(tmp_path / "enc.pdf")
        pdf.save(path, encryption=pikepdf.Encryption(
            user="", owner="owner",
            allow=pikepdf.Permissions(extract=False)))
        pdf.close()
        result = check_security(path)
        assert result["encrypted"] is True
        assert result["content_copy_allowed"] is False
        assert result["severity"] in ("warning", "error")
