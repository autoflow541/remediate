"""Abbreviation and acronym detection (WCAG 3.1.4 — Sprint 11).

WCAG 3.1.4 (Level AAA) recommends providing expansions for abbreviations.
While Level AA only requires it indirectly via 3.1.1/3.1.2, detecting
undefined abbreviations is valuable advisory information for remediators
working toward comprehensive accessibility.

Detection approach:
  • Find all-caps tokens of 2-5 characters (e.g. PDF, WCAG, HTML, PDF/UA)
  • Find dotted abbreviations (e.g. e.g., i.e., etc., U.S.A.)
  • Deduplicate and return sorted list

This is advisory — no automatic fix is applied.
"""

from __future__ import annotations

import re
from collections import Counter

# All-caps acronyms (2-5 letters, may contain / or &)
_ACRONYM = re.compile(r"\b([A-Z][A-Z0-9]{1,4}(?:[/&][A-Z0-9]{1,4})?)\b")
# Dotted abbreviations: e.g. U.S.A., i.e., e.g.
_DOTTED  = re.compile(r"\b([A-Za-z]{1,4}(?:\.[A-Za-z]{1,4}){1,4}\.?)\b")

# Common words that look like acronyms but aren't
_IGNORE = {
    "A", "I", "OK", "NO", "YES", "TO", "DO", "GO", "BE", "OR", "SO",
    "ON", "AT", "IN", "OF", "BY", "IS", "IT", "AS", "AN", "AM",
    "PDF", "URL", "HTTP", "HTML", "CSS", "XML", "JSON", "API",  # very common, rarely undefined
}


def detect_abbreviations(pdf_path: str, min_occurrences: int = 2) -> list[dict]:
    """Return list of abbreviations found in the PDF text.

    Each dict: {abbreviation, count, type, description}
    Sorted by count descending.  Only returns abbrevs appearing ≥ min_occurrences.
    """
    try:
        import fitz
    except ImportError:
        return []

    text_chunks: list[str] = []
    try:
        doc = fitz.open(pdf_path)
        for page in doc:
            text_chunks.append(page.get_text("text"))
        doc.close()
    except Exception:
        return []

    full_text = " ".join(text_chunks)

    counts: Counter = Counter()
    types: dict[str, str] = {}

    for m in _ACRONYM.finditer(full_text):
        token = m.group(1)
        if token not in _IGNORE and len(token) >= 2:
            counts[token] += 1
            types[token] = "acronym"

    for m in _DOTTED.finditer(full_text):
        token = m.group(1).lower()
        if len(token) >= 3 and "." in token:
            counts[token] += 1
            types[token] = "dotted"

    results = []
    for abbrev, count in counts.most_common():
        if count < min_occurrences:
            continue
        results.append({
            "abbreviation": abbrev,
            "count": count,
            "type": types.get(abbrev, "acronym"),
            "description": (
                f'"{abbrev}" appears {count} time{"s" if count != 1 else ""}. '
                "Consider adding an expansion on first use or providing a glossary "
                "(WCAG 3.1.4)."
            ),
        })

    return results
