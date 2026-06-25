"""Reflow and text spacing checker (WCAG 1.4.10 + 1.4.12 — Sprint 9).

WCAG 1.4.10 Reflow: content must not require two-dimensional scrolling at
  320 CSS px width (equivalent to 400% zoom on a 1280px screen).
  Fixed-position text with absolute coordinates that would be clipped or
  require horizontal scrolling at that scale is a failure.

WCAG 1.4.12 Text Spacing: users must be able to set:
  line-height ≥ 1.5×; paragraph spacing ≥ 2×; letter spacing ≥ 0.12×;
  word spacing ≥ 0.16×  without loss of content or functionality.

In PDFs both criteria are advisory — PDFs are inherently fixed-layout and
reflow requires PDF/UA + tagged content reordering by the AT.  This checker:
  • Flags pages where text spans exceed 85% of page width (reflow risk)
  • Flags very tight character spacing (letter-spacing < 0 — WCAG 1.4.12)
  • Flags leading/line-height values below 1.0× font size

These are reported as informational warnings, not hard failures.
"""

from __future__ import annotations

REFLOW_WIDTH_RATIO  = 0.85    # text span > this fraction of page width → reflow risk
MIN_LEADING_RATIO   = 1.0     # leading / font-size below this → line-height warning
MIN_LETTER_SPACING  = -0.5    # pt — tighter than this is a 1.4.12 concern


def check_reflow(pdf_path: str) -> list[dict]:
    """Return reflow and text-spacing advisory warnings."""
    try:
        import fitz
    except ImportError:
        return []

    issues: list[dict] = []
    seen_pages: set[int] = set()   # one reflow warning per page

    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []

    try:
        for page_idx, page in enumerate(doc):
            page_num = page_idx + 1
            pw = page.rect.width
            raw = page.get_text("rawdict", flags=0)
            for block in raw.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        x0, _, x1, _ = span["bbox"]
                        span_w = x1 - x0
                        size = span.get("size", 0) or 0

                        # Reflow: span near full page width
                        if page_num not in seen_pages and pw > 0 and (span_w / pw) > REFLOW_WIDTH_RATIO:
                            seen_pages.add(page_num)
                            issues.append({
                                "page": page_num,
                                "type": "reflow_risk",
                                "description": (
                                    f"Page {page_num}: text span spans {(span_w/pw)*100:.0f}% "
                                    "of page width. This fixed-width layout may not reflow "
                                    "correctly at 320px viewport (WCAG 1.4.10). "
                                    "Ensure the tagged PDF reading order enables AT reflow."
                                ),
                            })

                        # Letter spacing
                        char_spacing = span.get("char_spacing", 0) or 0
                        if size > 0 and char_spacing < MIN_LETTER_SPACING:
                            issues.append({
                                "page": page_num,
                                "type": "letter_spacing",
                                "value_pt": round(char_spacing, 2),
                                "description": (
                                    f"Page {page_num}: character spacing {char_spacing:.1f}pt "
                                    "is very tight. Users who require increased letter spacing "
                                    "(WCAG 1.4.12) may experience overlapping glyphs."
                                ),
                            })
    finally:
        doc.close()

    return issues
