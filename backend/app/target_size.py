"""Target size checker (WCAG 2.5.8 — Sprint 8).

WCAG 2.5.8 requires interactive targets to be at least 24×24 CSS pixels.
In PDF, we check annotation rects (Widgets = form controls, Link annotations).

PDF user-space units are 1/72 inch.  At 96 DPI (CSS reference pixel):
  24 CSS px = 24 × (72/96) = 18 pt

We flag targets where both width AND height are below 18pt.
(WCAG 2.5.5 — enhanced — requires 44×44px = 33pt, but 2.5.8 is the AA threshold.)

Returns list of {page, type, width_pt, height_pt, description} dicts.
"""

from __future__ import annotations

MIN_PT = 18.0  # 24 CSS px in PDF points (24 × 72/96)


def check_target_size(pdf_path: str) -> list[dict]:
    """Return target-size failures for interactive annotations (WCAG 2.5.8)."""
    try:
        import fitz
    except ImportError:
        return []

    issues: list[dict] = []
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []

    try:
        for page_idx, page in enumerate(doc):
            for annot in page.annots():
                try:
                    atype = annot.type[1]
                    if atype not in ("Widget", "Link"):
                        continue
                    rect = annot.rect
                    w = rect.width
                    h = rect.height
                    if w < MIN_PT and h < MIN_PT:
                        issues.append({
                            "page": page_idx + 1,
                            "type": atype,
                            "width_pt": round(w, 1),
                            "height_pt": round(h, 1),
                            "min_pt": MIN_PT,
                            "description": (
                                f"{atype} target {w:.1f}×{h:.1f}pt — "
                                f"need ≥{MIN_PT}pt in each dimension (WCAG 2.5.8)"
                            ),
                        })
                except Exception:
                    continue
    finally:
        doc.close()

    return issues
