"""rebuild_fallback.py — when repairing a tagged PDF can't reach conformance,
throw the broken tags away and rebuild from layout analysis.

The repair path preserves a document's existing structure tree, which is the
right call when that tree is sound. But some real-world PDFs are tagged so badly
that no mechanical repair can make them conformant — contradictory
artifact/tagged marked content, orphaned trees, structures veraPDF's own rule
engine trips on. For those, the winning move is the one the untagged pipeline
already does well: strip the structure entirely and rebuild it from
OpenDataLoader's layout analysis of the actual page content.

`rebuild_from_scratch()` does exactly that and returns the rebuilt file plus its
conformance result, so the caller can keep whichever of {repair, rebuild} is
better. Proven to take a pathological 122-failing-check document to full
PDF/UA-1 compliance.
"""

from __future__ import annotations

import logging
import os
import tempfile

log = logging.getLogger(__name__)


def strip_structure(in_path: str, out_path: str) -> None:
    """Write a copy of the PDF with its structure tagging removed, so the
    rebuild pipeline treats it as an untagged document."""
    import pikepdf

    with pikepdf.open(in_path) as pdf:
        cat = pdf.Root
        for key in ("/StructTreeRoot", "/MarkInfo"):
            if key in cat:
                del cat[key]
        for page in pdf.pages:
            for key in ("/StructParents",):
                if key in page.obj:
                    del page.obj[key]
            # drop per-annotation StructParent refs (now dangling)
            annots = page.obj.get("/Annots")
            if annots is not None:
                for a in annots:
                    try:
                        if hasattr(a, "get") and "/StructParent" in a:
                            del a["/StructParent"]
                    except Exception:
                        pass
        pdf.save(out_path)


def rebuild_from_scratch(in_path: str, flavour: str = "ua1"):
    """Strip + re-autotag + rebuild + deep-fix + embed fonts, then validate.

    Returns (out_path, ValidationResult, report) on success, or (None, None,
    None) if the rebuild could not be produced. The caller owns out_path and
    must delete it.
    """
    from .autotag import autotag_pdf
    from .writeback import remediate_pdf
    from .validate import safe_validate_pdf

    fd, stripped = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    fd, out_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        strip_structure(in_path, stripped)
        # Fresh layout manifest, uninfluenced by the discarded tags.
        manifest = autotag_pdf(stripped, detect_headers=True)
        report = remediate_pdf(stripped, manifest, out_path)
        report["mode"] = "rebuild-fallback"

        # Same mechanical closers the main pipeline uses.
        try:
            from .quickfix import run_deep_fix
            run_deep_fix(out_path)
        except Exception as exc:
            log.debug("rebuild_fallback deep_fix: %s", exc)
        try:
            from .font_check import check_fonts
            from .font_embed import embed_fonts
            embed_fonts(out_path, check_fonts(out_path))
        except Exception as exc:
            log.debug("rebuild_fallback font embed: %s", exc)

        result = safe_validate_pdf(out_path, flavour=flavour)
        return out_path, result, report
    except Exception as exc:
        log.warning("rebuild_fallback failed: %s", exc)
        if os.path.exists(out_path):
            os.unlink(out_path)
        return None, None, None
    finally:
        if os.path.exists(stripped):
            os.unlink(stripped)
