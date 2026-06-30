"""Link text checker — WCAG 2.4.4 (Link Purpose, In Context).

Identifies hyperlink annotations whose visible text is non-descriptive:
  • generic phrases like "click here", "here", "read more", "this link"
  • bare URLs used as display text (e.g. "https://example.com")
  • single characters or empty text
  • digits/punctuation only

Each finding includes page, the bad link text, the link URI (if any), and a
suggestion so the remediator knows what to fix.
"""

from __future__ import annotations

import re
from typing import Any

# ── Non-descriptive text patterns ─────────────────────────────────────────────
_GENERIC: list[re.Pattern] = [
    re.compile(r"^\s*click\s+here\s*\.?\s*$", re.I),
    re.compile(r"^\s*here\s*\.?\s*$", re.I),
    re.compile(r"^\s*read\s+more\s*\.?\s*$", re.I),
    re.compile(r"^\s*more\s*\.?\s*$", re.I),
    re.compile(r"^\s*learn\s+more\s*\.?\s*$", re.I),
    re.compile(r"^\s*this\s+(link|page|article|document|site)\s*\.?\s*$", re.I),
    re.compile(r"^\s*link\s*\.?\s*$", re.I),
    re.compile(r"^\s*see\s+more\s*\.?\s*$", re.I),
    re.compile(r"^\s*details?\s*\.?\s*$", re.I),
    re.compile(r"^\s*info(rmation)?\s*\.?\s*$", re.I),
    re.compile(r"^\s*more\s+info(rmation)?\s*\.?\s*$", re.I),
    re.compile(r"^\s*continue\s*\.?\s*$", re.I),
    re.compile(r"^\s*download\s*\.?\s*$", re.I),
    re.compile(r"^\s*source\s*\.?\s*$", re.I),
    re.compile(r"^\s*go\s*\.?\s*$", re.I),
    re.compile(r"^\s*view\s*\.?\s*$", re.I),
    re.compile(r"^\s*open\s*\.?\s*$", re.I),
    re.compile(r"^\s*visit\s*\.?\s*$", re.I),
    re.compile(r"^\s*click\s*\.?\s*$", re.I),
]

_URL_PATTERN = re.compile(r"^\s*https?://\S+\s*$", re.I)
_BARE_DOMAIN = re.compile(r"^\s*www\.\S+\s*$", re.I)


def _is_nondescriptive(text: str) -> tuple[bool, str]:
    """Return (True, reason) if the link text is non-descriptive."""
    t = text.strip()
    if not t:
        return True, "Empty link text"
    if len(t) <= 1:
        return True, "Single-character link text"
    if re.match(r"^[\d\W]+$", t):
        return True, "Numeric or punctuation-only link text"
    for pat in _GENERIC:
        if pat.match(t):
            return True, f"Generic non-descriptive link text: '{t}'"
    if _URL_PATTERN.match(t):
        return True, "Bare URL used as link text"
    if _BARE_DOMAIN.match(t):
        return True, "Bare domain used as link text"
    return False, ""


def check_link_text(pdf_path: str) -> list[dict[str, Any]]:
    """Return a list of non-descriptive link issues.

    Each issue dict:
        page        — 1-based page number
        linkText    — the visible link text (first 120 chars)
        uri         — the link URI if available, else None
        reason      — why this is flagged
        description — human-readable WCAG reference
        suggestion  — recommended fix
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return []

    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []

    issues: list[dict] = []
    seen: set[tuple] = set()

    try:
        for page_num, page in enumerate(doc, start=1):
            links = page.get_links()
            for link in links:
                uri = link.get("uri") or link.get("page")
                rect = fitz.Rect(link.get("from", fitz.Rect()))

                # Extract the visible text within the link rect
                link_text = ""
                try:
                    words = page.get_text("words", clip=rect)
                    link_text = " ".join(w[4] for w in words).strip()
                except Exception:
                    pass

                # If no text in rect, try to get /Contents from annotation
                if not link_text:
                    try:
                        for annot in page.annots():
                            arect = annot.rect
                            if abs(arect.x0 - rect.x0) < 2 and abs(arect.y0 - rect.y0) < 2:
                                link_text = annot.info.get("content", "") or ""
                                break
                    except Exception:
                        pass

                if not link_text:
                    link_text = str(uri or "")[:80]

                bad, reason = _is_nondescriptive(link_text)
                if not bad:
                    continue

                dedup = (page_num, link_text[:40].lower())
                if dedup in seen:
                    continue
                seen.add(dedup)

                uri_str = str(uri) if uri else None
                issues.append({
                    "page": page_num,
                    "linkText": link_text[:120],
                    "uri": uri_str[:200] if uri_str else None,
                    "reason": reason,
                    "description": (
                        "WCAG 2.4.4 requires that the purpose of each link can be "
                        "determined from the link text alone, or from the link text "
                        "together with its programmatically determined context."
                    ),
                    "suggestion": (
                        "Replace the link text with a concise, descriptive phrase that "
                        "identifies the link destination or function, e.g. "
                        "'Download the 2024 Annual Report (PDF)' instead of 'click here'."
                    ),
                })
    finally:
        doc.close()

    return issues
