"""XFA form detection (Sprint 8).

Dynamic XFA (XML Forms Architecture) forms are a separate PDF sub-format that
cannot be remediated with standard struct-tree techniques.  AT (NVDA, JAWS)
uses a completely different rendering path for XFA.

This module checks for the presence of /XFA in the AcroForm dictionary and
returns an advisory warning when found, so the remediator knows the form
needs a different approach (export to HTML5 / PDF AcroForm conversion).
"""

from __future__ import annotations


def detect_xfa(pdf_path: str) -> dict | None:
    """Return an XFA warning dict if the PDF contains an XFA form, else None."""
    try:
        import pikepdf
    except ImportError:
        return None

    try:
        with pikepdf.open(pdf_path) as pdf:
            acroform = pdf.Root.get("/AcroForm")
            if not acroform:
                return None
            xfa = acroform.get("/XFA")
            if xfa is None:
                return None
            return {
                "detected": True,
                "description": (
                    "This PDF contains an XFA (XML Forms Architecture) dynamic form. "
                    "XFA forms require specialised AT support that differs from standard "
                    "PDF/UA tagged forms. Full accessibility remediation requires converting "
                    "the XFA form to AcroForm or an accessible HTML5 equivalent. "
                    "Structure-tree tagging applied by this engine may not be sufficient."
                ),
            }
    except Exception:
        return None
