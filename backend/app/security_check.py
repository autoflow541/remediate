"""PDF security/permissions check -- Sprint 25 (PDF/UA 7.16)."""
from __future__ import annotations
import logging
log = logging.getLogger(__name__)


def check_security(pdf_path: str) -> dict:
    """Check PDF encryption and accessibility permissions.

    Returns dict with keys:
      encrypted, accessibility_allowed, content_copy_allowed, severity, description
    """
    result = {
        "encrypted": False,
        "accessibility_allowed": True,
        "content_copy_allowed": True,
        "severity": "ok",
        "description": "No encryption -- content fully accessible.",
    }
    try:
        import pikepdf
        pdf = pikepdf.open(pdf_path)
        try:
            enc = pdf.encryption
            try:
                _ = enc.R   # KeyError if not encrypted
            except (KeyError, AttributeError):
                return result
            result["encrypted"] = True
            allow = pdf.allow
            result["accessibility_allowed"] = bool(getattr(allow, "accessibility", True))
            result["content_copy_allowed"] = bool(getattr(allow, "extract", True))
            if not result["accessibility_allowed"]:
                result["severity"] = "error"
                result["description"] = (
                    "Encrypted: accessibility permission bit NOT set. "
                    "PDF/UA-1 s7.16 violation."
                )
            elif not result["content_copy_allowed"]:
                result["severity"] = "warning"
                result["description"] = "Encrypted; text copy restricted but AT can read."
            else:
                result["description"] = "Encrypted but accessibility permission set."
        finally:
            pdf.close()
    except Exception as exc:
        log.debug("security_check: %s", exc)
        result["description"] = "Check failed: " + str(exc)
    return result
