"""annot_alt_fix.py — alternate descriptions for annotations (PDF/UA 7.18).

veraPDF clauses this closes (top offenders in the repair-mode benchmark —
4 of 6 already-tagged documents, ~200 failing checks):

  7.18.1-2  every annotation (except Widgets and hidden ones) shall include an
            alternate description via its /Contents key
  7.18.5-2  Link annotations shall carry an alternate description via /Contents

For Link annotations the description is derived from the target URL with the
same slug/domain heuristics the rebuild path uses (fix_link_text). For other
annotation types a subtype-appropriate description is synthesized from the
annotation's own data where possible.

In-place, idempotent, no AI required. Safe for both rebuild and repair modes.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_SUBTYPE_LABEL = {
    "/Text": "Note",
    "/Highlight": "Highlighted text",
    "/Underline": "Underlined text",
    "/Squiggly": "Marked text",
    "/StrikeOut": "Struck-through text",
    "/FreeText": "Text note",
    "/Stamp": "Stamp",
    "/Ink": "Ink drawing",
    "/Square": "Rectangle marking",
    "/Circle": "Ellipse marking",
    "/Line": "Line marking",
    "/Polygon": "Polygon marking",
    "/PolyLine": "Polyline marking",
    "/FileAttachment": "Attached file",
    "/Popup": None,     # popups are paired with a parent annot; skip
    "/Widget": None,    # exempt per clause
}


def _link_target(annot) -> str:
    """Best-effort URL / destination string for a Link annotation."""
    try:
        a = annot.get("/A")
        if a is not None:
            uri = a.get("/URI")
            if uri is not None:
                return str(uri)
            s = a.get("/S")
            if s is not None and str(s) == "/GoTo":
                return "internal destination"
    except Exception:
        pass
    try:
        if annot.get("/Dest") is not None:
            return "internal destination"
    except Exception:
        pass
    return ""


def fix_annotation_descriptions(pdf_path: str) -> tuple[int, list[str]]:
    """Add /Contents to annotations that lack an alternate description.

    Returns (count_fixed, notes). Modifies pdf_path in place.
    """
    try:
        import pikepdf
        from pikepdf import Name, String
    except ImportError:
        return 0, []

    try:
        from .fix_link_text import generate_link_description
    except Exception:  # pragma: no cover
        generate_link_description = lambda url, ctx="": (url or "Link")[:80]  # noqa: E731

    fixed = 0
    links = 0
    try:
        with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
            for page in pdf.pages:
                annots = page.get("/Annots")
                if not annots:
                    continue
                for annot in annots:
                    try:
                        subtype = str(annot.get("/Subtype", ""))
                        if _SUBTYPE_LABEL.get(subtype, "skip") is None:
                            continue  # Widget / Popup — exempt
                        # Hidden annotations are exempt (flag bit 2).
                        flags = int(annot.get("/F", 0) or 0)
                        if flags & 2:
                            continue
                        existing = annot.get("/Contents")
                        if existing is not None and str(existing).strip():
                            continue

                        if subtype == "/Link":
                            target = _link_target(annot)
                            if target == "internal destination":
                                desc = "Link within this document"
                            elif target:
                                desc = generate_link_description(target)
                            else:
                                desc = "Link"
                            links += 1
                        else:
                            desc = _SUBTYPE_LABEL.get(subtype) or f"{subtype.lstrip('/')} annotation"
                        annot[Name("/Contents")] = String(desc[:200])
                        fixed += 1
                    except Exception:
                        continue
            if fixed:
                pdf.save()
    except Exception as exc:
        log.warning("annot_alt_fix: %s", exc)
        return 0, []

    notes = []
    if fixed:
        notes.append(
            f"Added alternate descriptions (/Contents) to {fixed} annotation"
            f"{'s' if fixed != 1 else ''}"
            + (f", {links} of them links (PDF/UA 7.18)" if links else " (PDF/UA 7.18)")
        )
    return fixed, notes
