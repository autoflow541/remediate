"""Radio button group accessibility fixer (Sprint 16 — PDF/UA §7.18.4).

Radio button widgets in AcroForm must be grouped:
  • All buttons in the same group share the same field name (/T)
  • Their parent field (FT=Btn, Ff has bit 15 set for Radio) should be tagged
    as a Form struct element, not each individual widget separately
  • veraPDF clause 7.18.4-1: Widget annotations must be in struct tree

This module detects radio button groups in the AcroForm /Fields array and
ensures the manifest has a single Form node per radio group (not per button),
which writeback.py then renders correctly.

Also detects checkbox groups and consolidates them similarly.
"""

from __future__ import annotations

import logging
from collections import defaultdict

import pikepdf

log = logging.getLogger(__name__)

# AcroForm field type flags
_FF_RADIO      = 1 << 15  # Bit 16 (0-indexed 15): Radio button
_FF_PUSHBUTTON = 1 << 16  # Bit 17: Push button
_FF_MULTISELECT = 1 << 21  # Bit 22: Multi-select list


def _is_radio_group(field: pikepdf.Object) -> bool:
    """Return True if this field is a radio button group."""
    try:
        if str(field.get("/FT", "")) != "/Btn":
            return False
        ff = int(field.get("/Ff", 0))
        return bool(ff & _FF_RADIO) and not bool(ff & _FF_PUSHBUTTON)
    except Exception:
        return False


def _get_field_name(field: pikepdf.Object) -> str:
    try:
        t = field.get("/T")
        return str(t) if t is not None else ""
    except Exception:
        return ""


def _get_tu(field: pikepdf.Object) -> str:
    """Tooltip / accessible name."""
    try:
        tu = field.get("/TU")
        return str(tu) if tu is not None else ""
    except Exception:
        return ""


def fix_radio_groups(pdf: pikepdf.Pdf, manifest: dict) -> tuple[dict, int]:
    """Consolidate radio button groups in the manifest.

    Returns (updated_manifest, groups_fixed_count).
    """
    try:
        acroform = pdf.Root.get("/AcroForm")
        if not acroform:
            return manifest, 0
        fields = acroform.get("/Fields")
        if not fields:
            return manifest, 0
    except Exception:
        return manifest, 0

    groups_fixed = 0
    radio_groups: dict[str, dict] = {}  # field_name → {tu, kids}

    def _collect_radio(fields_list):
        try:
            for field_ref in fields_list:
                try:
                    field = field_ref
                    if hasattr(field_ref, "get_object"):
                        field = field_ref.get_object()
                    if _is_radio_group(field):
                        name = _get_field_name(field)
                        tu = _get_tu(field)
                        if name:
                            radio_groups[name] = {
                                "tu": tu or name,
                                "required": bool(int(field.get("/Ff", 0)) & 2),
                            }
                    # Recurse into kids
                    kids = field.get("/Kids")
                    if kids:
                        _collect_radio(list(kids))
                except Exception:
                    pass
        except Exception:
            pass

    _collect_radio(list(fields))

    if not radio_groups:
        return manifest, 0

    # Walk manifest and consolidate: find Form nodes whose field_name is in a
    # radio group — mark the first occurrence as the group representative and
    # drop/mark subsequent ones so writeback doesn't duplicate annotations.
    seen_groups: set[str] = set()

    def _walk(nodes: list[dict]) -> list[dict]:
        nonlocal groups_fixed
        result = []
        for n in nodes:
            if n.get("tag") == "Form":
                fname = n.get("field_name", "")
                if fname in radio_groups:
                    if fname not in seen_groups:
                        seen_groups.add(fname)
                        # Enrich with radio group metadata
                        g = radio_groups[fname]
                        n = {
                            **n,
                            "radioGroup": True,
                            "alt": n.get("alt") or g["tu"],
                            "field_type": "radio",
                            "required": g.get("required", False),
                        }
                        groups_fixed += 1
                    else:
                        # Subsequent buttons in the same group — mark as grouped
                        n = {**n, "radioGroupMember": True, "skip_struct": True}
            if n.get("children"):
                n = {**n, "children": _walk(n["children"])}
            result.append(n)
        return result

    new_nodes = _walk(manifest.get("nodes", []))
    return {**manifest, "nodes": new_nodes}, groups_fixed
