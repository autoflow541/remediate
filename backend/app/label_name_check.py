"""WCAG 2.5.3 Label in Name checker.

WCAG 2.5.3 requires that for UI components with visible text labels, the
accessible name contains the visible text (case-insensitive).  In PDFs,
this means form fields whose visible adjacent label text is not present in
their /TU (tooltip / accessible name) attribute.

When the visible label says "First name" but /TU is "field_1" or empty,
voice control users saying "click First name" cannot activate the field.

This module:
  1. Enumerates AcroForm fields via pikepdf.
  2. For each field, extracts the accessible name (/TU then /T).
  3. Uses PyMuPDF to find visible text near the field annotation's bbox
     on the same page (within LABEL_SEARCH_MARGIN points on left or above).
  4. Checks whether the visible label text appears (case-insensitive,
     normalised whitespace) in the accessible name.
  5. Returns a list of advisory warnings for any mismatch found.

Each warning dict:
  {field_name, accessible_name, visible_label, page, issue, description}
"""

from __future__ import annotations

import re
import unicodedata

LABEL_SEARCH_MARGIN = 120.0   # pt — how far left/above field to look for label
MIN_LABEL_LEN       = 2       # ignore single-char "labels"


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip().lower()


def _contains_label(accessible_name: str, label: str) -> bool:
    """Return True if accessible_name contains the label text."""
    an = _normalise(accessible_name)
    lb = _normalise(label)
    if not lb or len(lb) < MIN_LABEL_LEN:
        return True  # too short to meaningfully check
    return lb in an


def _find_visible_label(fitz_page, field_bbox: tuple, page_height: float) -> str:
    """Find text near a field's bbox that might be its visible label."""
    fx0, fy0, fx1, fy1 = field_bbox  # fitz coords (top-left origin)

    # Search region: to the left and above the field
    search_x0 = max(0, fx0 - LABEL_SEARCH_MARGIN)
    search_y0 = max(0, fy0 - LABEL_SEARCH_MARGIN)
    search_x1 = fx0 + 4   # small overlap to capture inline labels
    search_y1 = fy1 + 4

    import fitz as _fitz
    rect = _fitz.Rect(search_x0, search_y0, search_x1, search_y1)
    text = fitz_page.get_text("text", clip=rect).strip()

    # Also search directly above (wider horizontal, tight vertical)
    above_rect = _fitz.Rect(fx0 - 8, max(0, fy0 - 40), fx1 + 8, fy0 + 2)
    above_text = fitz_page.get_text("text", clip=above_rect).strip()

    combined = " ".join(filter(None, [text, above_text]))
    # Take last 60 chars (closest text to the field)
    return combined[-60:].strip()


def check_label_in_name(pdf_path: str) -> list[dict]:
    """Check WCAG 2.5.3: visible label must be contained in accessible name.

    Returns a list of advisory warning dicts (empty = no issues found).
    Degrades gracefully if pikepdf or fitz are not available.
    """
    try:
        import pikepdf
        import fitz
    except ImportError:
        return []

    warnings: list[dict] = []

    try:
        pdf = pikepdf.open(pdf_path)
    except Exception:
        return []

    try:
        acroform = pdf.Root.get("/AcroForm")
        if not acroform:
            return []
        fields = acroform.get("/Fields")
        if not fields:
            return []

        # Build page → fitz page map (open fitz lazily)
        fitz_doc = fitz.open(pdf_path)

        # Build page object id → fitz page index
        page_id_map: dict[int, int] = {}
        for idx, page in enumerate(pdf.pages):
            page_id_map[id(page.obj)] = idx

        def _check_field(field_obj):
            """Recursively check a field (handles field hierarchy)."""
            try:
                # Check kids first (field groups)
                kids = field_obj.get("/Kids")
                if kids:
                    for kid in kids:
                        _check_field(kid)
                    # If this is a non-terminal node (no /Subtype), skip own check
                    if not field_obj.get("/Subtype"):
                        return

                ft = field_obj.get("/FT")
                if ft is None:
                    return  # not a terminal field

                # Accessible name: prefer /TU over /T
                tu = field_obj.get("/TU")
                t  = field_obj.get("/T")
                accessible_name = str(tu or t or "").strip()
                field_name = str(t or "").strip()

                if not accessible_name:
                    return  # no accessible name to check

                # Find which page this field's widget annotation is on
                subtype = field_obj.get("/Subtype")
                rect_val = field_obj.get("/Rect")
                pg_obj   = field_obj.get("/P")

                if not rect_val or not pg_obj:
                    return

                page_idx = page_id_map.get(id(pg_obj.obj if hasattr(pg_obj, "obj") else pg_obj))
                if page_idx is None:
                    return

                fitz_page = fitz_doc[page_idx]
                rect = [float(v) for v in rect_val]
                # pikepdf rect is [x0, y0, x1, y1] in PDF coords (bottom-left origin)
                # Convert to fitz (top-left origin)
                page_h = fitz_page.rect.height
                fitz_rect = (rect[0], page_h - rect[3], rect[2], page_h - rect[1])

                visible_label = _find_visible_label(fitz_page, fitz_rect, page_h)

                if not visible_label or len(visible_label.strip()) < MIN_LABEL_LEN:
                    return  # no visible label found nearby — skip

                if _contains_label(accessible_name, visible_label):
                    return  # PASS

                warnings.append({
                    "field_name": field_name,
                    "accessible_name": accessible_name,
                    "visible_label": visible_label.strip()[-80:],
                    "page": page_idx + 1,
                    "issue": "label_not_in_name",
                    "description": (
                        f"Visible label \"{visible_label.strip()[:40]}\" is not contained "
                        f"in the accessible name \"{accessible_name[:40]}\". "
                        "Voice control users may not be able to activate this field "
                        "by speaking its visible label (WCAG 2.5.3)."
                    ),
                })
            except Exception:
                pass

        for field_ref in fields:
            try:
                _check_field(field_ref)
            except Exception:
                continue

    finally:
        try:
            fitz_doc.close()
        except Exception:
            pass
        pdf.close()

    return warnings
