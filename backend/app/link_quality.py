"""WCAG 2.4.4 — Link Purpose: flag non-descriptive link text in PDFs.

A link passes 2.4.4 if its text alone (or in context) describes the
destination.  "Click here", "Read more", bare URLs, and single-character
labels are the four most common failure patterns in faculty-uploaded
university PDFs.

Returns a list of {page, text, url, issue, bbox} dicts so the studio can
surface them as warnings and include them in the audit report.
"""

from __future__ import annotations

import re

# Non-descriptive link texts (lower-cased, stripped).
_GENERIC: set[str] = {
    "click here", "click", "here", "read more", "more", "more info",
    "more information", "learn more", "link", "this link", "this",
    "go", "go here", "visit", "visit here", "download", "get it",
    "open", "view", "see more", "continue", "continue reading",
    "see here", "follow this link", "follow", "press here", "tap here",
    "source", "reference", "ref", "details", "info", "information",
    "page", "website", "web page", "site", "url", "link here",
}

_URL_RE = re.compile(r"^https?://|^www\.", re.IGNORECASE)
_SHORT_WORD_RE = re.compile(r"^\w{1,2}$")   # single or 2-letter words

# Generic link text that starts with one of these phrases should also be flagged.
# Catches "click here for more information", "click here to download", etc.
_GENERIC_STARTS: tuple[str, ...] = (
    "click here", "go here", "see here", "visit here",
    "read more", "learn more", "find out more", "see more",
    "press here", "tap here", "follow this link",
    "for more information", "for more details", "for details",
)


def _is_generic(text: str) -> bool:
    """Return True if the normalised link text is non-descriptive."""
    if text in _GENERIC:
        return True
    return any(text.startswith(prefix) for prefix in _GENERIC_STARTS)


def check_link_quality(pdf_path: str) -> list[dict]:
    """Return a list of link quality failures for all hyperlinks in the PDF."""
    try:
        import fitz
    except ImportError:
        return []

    issues: list[dict] = []
    doc = fitz.open(pdf_path)
    try:
        for page_num, page in enumerate(doc):
            for link in page.get_links():
                uri = link.get("uri", "")
                if not uri:
                    continue
                rect = link.get("from")
                if rect is None:
                    continue

                link_text = page.get_text("text", clip=rect).strip()
                if not link_text:
                    continue

                normalized = link_text.lower().strip().rstrip(".,;:")
                issue = None

                if _is_generic(normalized):
                    issue = f'Non-descriptive link text: "{link_text}"'
                elif _URL_RE.match(link_text):
                    issue = f'URL used as link text: "{link_text[:80]}"'
                elif _SHORT_WORD_RE.match(normalized):
                    issue = f'Link text too short to be descriptive: "{link_text}"'

                if issue:
                    issues.append({
                        "page": page_num + 1,
                        "text": link_text[:120],
                        "url": uri[:200],
                        "issue": issue,
                        "bbox": list(rect),
                    })
    finally:
        doc.close()

    return issues
