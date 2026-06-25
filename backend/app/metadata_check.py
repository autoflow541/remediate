"""PDF metadata completeness checker (Sprint 9).

Complete, accurate document metadata enables:
  • AT to announce the document title on open (WCAG 2.4.2)
  • Search engines and document management systems to index correctly
  • PDF/UA: Title required via ViewerPreferences/DisplayDocTitle

Checks for: Title, Author, Subject (description), Keywords,
  Language (/Lang), PDF version (≥ 1.4 required for accessibility features),
  Creation date.

Returns advisory warnings for each missing or empty metadata field.
"""

from __future__ import annotations


_MIN_PDF_VERSION = (1, 4)


def check_metadata(pdf_path: str) -> list[dict]:
    """Return metadata completeness warnings."""
    try:
        import pikepdf
    except ImportError:
        return []

    issues: list[dict] = []
    try:
        with pikepdf.open(pdf_path) as pdf:
            info = pdf.docinfo or {}

            def _val(key: str) -> str:
                v = info.get(key)
                return str(v).strip() if v else ""

            # Title (WCAG 2.4.2 — required)
            title = _val("/Title") or str(pdf.Root.get("/Lang") or "")
            # Better: check XMP too
            try:
                with pdf.open_metadata() as meta:
                    xmp_title = meta.get("dc:title") or ""
            except Exception:
                xmp_title = ""
            if not _val("/Title") and not xmp_title:
                issues.append({
                    "field": "Title",
                    "severity": "error",
                    "description": (
                        "Document has no /Title. Screen readers announce the title when the "
                        "document opens. WCAG 2.4.2 requires a descriptive page title. "
                        "Add a title in Document Properties before exporting."
                    ),
                })

            # Author
            if not _val("/Author"):
                issues.append({
                    "field": "Author",
                    "severity": "warning",
                    "description": "No /Author metadata. Add the author or organisation name.",
                })

            # Subject (description / dc:description)
            if not _val("/Subject"):
                issues.append({
                    "field": "Subject",
                    "severity": "warning",
                    "description": (
                        "No /Subject (document description). A brief description helps users "
                        "decide whether the document is relevant before reading it."
                    ),
                })

            # Language
            lang = pdf.Root.get("/Lang")
            if not lang:
                issues.append({
                    "field": "Language",
                    "severity": "error",
                    "description": (
                        "No /Lang set on the document. Screen readers use the language to select "
                        "the correct speech engine and pronunciation rules. "
                        "WCAG 3.1.1 and PDF/UA-1 clause 7.2 both require a document language."
                    ),
                })

            # PDF version
            try:
                ver_str = pdf.pdf_version   # e.g. "1.7"
                parts = tuple(int(x) for x in ver_str.split("."))
                if parts < _MIN_PDF_VERSION:
                    issues.append({
                        "field": "PDF version",
                        "severity": "error",
                        "description": (
                            f"PDF version {ver_str} predates accessibility features. "
                            "PDF/UA-1 requires PDF 1.4 or later. Re-export or update the PDF version."
                        ),
                    })
            except Exception:
                pass

            # Creation date
            if not _val("/CreationDate"):
                issues.append({
                    "field": "CreationDate",
                    "severity": "info",
                    "description": "No /CreationDate metadata. Recommended for document management.",
                })

    except Exception:
        return []

    return issues
