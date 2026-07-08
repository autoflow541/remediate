"""tagged_check.py — detect already-tagged PDFs and repair without rebuilding.

The rebuild pipeline (autotag -> writeback) is correct for *untagged* PDFs, but
destructive for PDFs that already carry a good structure tree: it replaces the
existing tags with a coarser auto-generated tree and can leave real content
untagged or swept into /Artifact — making an already-conformant file *less*
accessible.

This module adds the missing branch:

  * ``assess_tagging(pdf_path)``  — is the PDF already meaningfully tagged?
  * ``repair_tagged(pdf_path, out_path, manifest)`` — apply only the mechanical,
    catalog-level PDF/UA fixes (MarkInfo, DisplayDocTitle, Lang, XMP pdfuaid,
    Title) while **preserving the existing structure tree**. The endpoint's
    downstream non-destructive fixers (font embedding, scope, veraPDF
    auto-repair) then run on top.

The regression guard (`regressed`) is the belt-and-suspenders: if the output has
more failing checks than the input, the caller returns the original untouched.
"""

from __future__ import annotations

import logging

import pikepdf

log = logging.getLogger(__name__)

# A StructTreeRoot with fewer than this many structure elements is treated as a
# stub (some producers emit an empty/near-empty tree). Real tagged documents
# have far more; untagged documents have none.
MIN_STRUCT_ELEMENTS = 5


def _count_struct_elements(struct_root, cap: int = 20000) -> int:
    """Count structure elements under a StructTreeRoot, with cycle protection.

    A structure element is identified by its ``/S`` (structure type) key — NOT
    by ``/Type /StructElem``, which is *optional* in PDF and routinely omitted.
    Marked-content refs (/MCR) and object refs (/OBJR) have no /S and don't count.
    """
    if struct_root is None:
        return 0
    seen: set = set()
    count = 0

    def _children(obj):
        try:
            k = obj.get("/K")
        except Exception:
            return []
        if k is None:
            return []
        return list(k) if isinstance(k, pikepdf.Array) else [k]

    stack = _children(struct_root)
    while stack and count < cap:
        node = stack.pop()
        # Only dictionary-like nodes have /S; ints (MCIDs), names, strings don't.
        try:
            s = node.get("/S")
        except Exception:
            continue
        try:
            og = node.objgen
            if og != (0, 0):
                if og in seen:
                    continue
                seen.add(og)
        except Exception:
            pass
        if s is not None:
            count += 1
        stack.extend(_children(node))
    return count


def assess_tagging(pdf_path: str) -> dict:
    """Return {tagged, elements, marked, hasStructRoot, reason}.

    ``tagged`` is True only when the PDF has a StructTreeRoot with a non-trivial
    number of structure elements — i.e. rebuilding it would risk destroying real
    tagging.
    """
    info = {
        "tagged": False,
        "elements": 0,
        "marked": False,
        "hasStructRoot": False,
        "reason": "",
    }
    try:
        with pikepdf.open(pdf_path) as pdf:
            struct_root = pdf.Root.get("/StructTreeRoot")
            info["hasStructRoot"] = struct_root is not None
            mark_info = pdf.Root.get("/MarkInfo")
            info["marked"] = bool(mark_info is not None and bool(mark_info.get("/Marked", False)))
            info["elements"] = _count_struct_elements(struct_root)
    except Exception as exc:
        info["reason"] = f"assessment failed: {exc}"
        return info

    if info["hasStructRoot"] and info["elements"] >= MIN_STRUCT_ELEMENTS:
        info["tagged"] = True
        info["reason"] = f"existing structure tree with {info['elements']} elements — preserve"
    else:
        info["reason"] = (
            "no meaningful structure tree — safe to rebuild"
            if not info["hasStructRoot"]
            else f"structure tree is a stub ({info['elements']} elements) — rebuild"
        )
    return info


def repair_tagged(pdf_path: str, out_path: str, manifest: dict) -> dict:
    """Repair an already-tagged PDF in place-to-out_path, preserving its
    structure tree. Applies only the catalog-level PDF/UA fixes; downstream
    fixers in the endpoint handle fonts/scope/etc.
    """
    from .pdf_ua_comply import write_pdfua_metadata

    report: dict = {"mode": "repair", "elements": 0, "mcids": 0}
    try:
        with pikepdf.open(pdf_path) as pdf:
            struct_root = pdf.Root.get("/StructTreeRoot")
            report["elements"] = _count_struct_elements(struct_root)
            report["pdfuaMetadata"] = write_pdfua_metadata(pdf, manifest or {})
            pdf.save(out_path)
    except Exception as exc:
        log.warning("repair_tagged failed (%s); falling back to copy", exc)
        import shutil
        shutil.copyfile(pdf_path, out_path)
        report["error"] = str(exc)
    return report
