"""Annotation accessibility checker (Sprint 15).

PDF/UA-1 clause 7.18 requires all annotation types to be accessible:
  - 7.18.1: Every annotation (except Widget, Link, PrinterMark) needs /Contents
  - 7.18.3: /Tab /S on every page with annotations
  - 7.18.5: Link annotations must have /Contents or /Alt (in struct)
  - 7.18.6: Annotation flag /Hidden and /Invisible must not be set on real content

This module:
  1. Checks all non-Link, non-Widget annotations for /Contents
  2. Auto-patches missing /Contents with a sensible default (type + subtype)
  3. Returns advisory warnings for annotations it couldn't fix
"""

from __future__ import annotations

import logging

import pikepdf
from pikepdf import Name, String

log = logging.getLogger(__name__)

# Annotation types that require /Contents per PDF/UA-1 §7.18
_REQUIRE_CONTENTS = {
    "/Text", "/FreeText", "/Line", "/Square", "/Circle", "/Polygon",
    "/PolyLine", "/Highlight", "/Underline", "/Squiggly", "/StrikeOut",
    "/Stamp", "/Caret", "/Ink", "/Popup", "/FileAttachment", "/Sound",
    "/Movie", "/Screen", "/TrapNet", "/Watermark", "/3D",
}

# Human-readable names for annotation subtypes
_ANNOT_LABELS = {
    "/Text": "Comment annotation",
    "/FreeText": "Free text annotation",
    "/Line": "Line annotation",
    "/Square": "Rectangle annotation",
    "/Circle": "Ellipse annotation",
    "/Polygon": "Polygon shape annotation",
    "/PolyLine": "Polyline annotation",
    "/Highlight": "Highlighted text",
    "/Underline": "Underlined text",
    "/Squiggly": "Squiggly underline annotation",
    "/StrikeOut": "Strikethrough annotation",
    "/Stamp": "Rubber stamp annotation",
    "/Caret": "Caret annotation",
    "/Ink": "Ink (freehand) annotation",
    "/FileAttachment": "Attached file",
    "/Sound": "Sound clip",
    "/Movie": "Movie clip",
    "/Screen": "Screen annotation",
    "/Watermark": "Watermark annotation",
}


def fix_annotation_contents(pdf: pikepdf.Pdf) -> tuple[int, list[dict]]:
    """Auto-patch annotations missing /Contents and return advisory warnings.

    Returns (fixed_count, remaining_issues).
    """
    fixed = 0
    issues: list[dict] = []

    for page_num, page in enumerate(pdf.pages, start=1):
        annots = page.get("/Annots")
        if not annots:
            continue
        try:
            annots = list(annots)
        except Exception:
            continue

        for annot_ref in annots:
            try:
                annot = annot_ref
                if hasattr(annot_ref, "get_object"):
                    annot = annot_ref.get_object()

                subtype = str(annot.get("/Subtype", "")).strip()

                if subtype not in _REQUIRE_CONTENTS:
                    continue  # Link/Widget handled elsewhere; others skipped

                # Check for hidden/invisible flags (bits 1 and 2 of /F)
                flags = int(annot.get("/F", 0))
                if flags & 0b11:  # Hidden(1) or Invisible(2)
                    continue  # Screen reader won't see it anyway

                has_contents = "/Contents" in annot
                if has_contents:
                    # Check it's non-empty
                    contents_str = str(annot["/Contents"]).strip()
                    if contents_str:
                        continue

                # Auto-generate a default /Contents
                label = _ANNOT_LABELS.get(subtype, f"{subtype.lstrip('/')} annotation")

                # Try to use existing text for rich annotations
                if subtype == "/Text" and "/T" in annot:
                    label = f"Note by {annot['/T']}"
                elif subtype in ("/Highlight", "/Underline", "/StrikeOut", "/Squiggly"):
                    # These annotate existing text — try to get /QuadPoints extent
                    label = f"{label} — see surrounding text"

                try:
                    annot[Name("/Contents")] = String(label)
                    fixed += 1
                    log.debug("annot_check: patched /Contents on %s pg%d", subtype, page_num)
                except Exception as exc:
                    issues.append({
                        "page": page_num,
                        "type": subtype.lstrip("/"),
                        "severity": "warning",
                        "description": (
                            f"Could not set /Contents on {label} (page {page_num}): {exc}. "
                            "Add a text description manually (PDF/UA §7.18.1)."
                        ),
                    })

            except Exception as exc:
                log.debug("annot_check: error on page %d: %s", page_num, exc)

    return fixed, issues
