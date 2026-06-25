"""PDF encryption / permissions accessibility check — Sprint 25 (PDF/UA 7.16).

PDF/UA-1 §7.16: if a document is encrypted, the accessibility permission
(bit 10 of the P entry — "ExtractTextAndGraphics for Accessibility") must
be set. Without it, screen readers cannot extract text from the PDF.

Returns a structured result dict rather than raising exceptions so that
the main endpoint can include it in the conformance report.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def check_security(pdf_path: str) -> dict:
    """Check PDF encryption and accessibility permissions.

    Returns:
      {
        "encrypted": bool,
        "accessibility_allowed": bool,
        "content_copy_allowed": bool,
        "severity": "ok" | "warning" | "error",
        "description": str,
      }
    """
    result: dict = {
        "encrypted": False,
        "accessibility_allowed": True,
        "content_copy_allowed": True,
        "severity": "ok",
        "description": "No encryption — content fully accessible.",
    }

    try:
        import pikepdf
        pdf = pikepdf.open(pdf_path)
        try:
            enc = pdf.encryption
            if not enc:
                return result

            result["encrypted"] = True
            allow = pdf.allow
            # pikepdf exposes AllowedOperations as named attributes
            result["accessibility_allowed"] = bool(
                getattr(allow, "accessibility", True)
            )
            result["content_copy_allowed"] = bool(
                getattr(allow, "extract", True)
            )

            if not result["accessibility_allowed"]:
                result["severity"] = "error"
                result["description"] = (
                    "PDF is encrypted and the accessibility permission bit is NOT set. "
                    "Screen readers cannot extract text — this is a PDF/UA-1 §7.16 violation."
                )
            elif not result["content_copy_allowed"]:
                result["severity"] = "warning"
                result["description"] = (
                    "PDF is encrypted; text copy is restricted but the accessibility "
                    "permission is set. Verify that assistive technology can read content."
                )
            else:
                result["description"] = (
                    "PDF is encrypted but the accessibility permission is set — AT can read it."
                )
        finally:
            pdf.close()
    except Exception as exc:
        log.debug("security_check: %s", exc)
        result["description"] = f"Security check could not be completed: {exc}"

    return result
