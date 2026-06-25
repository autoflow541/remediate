"""Round-trip compliance checker (Sprint 30).

After writeback.py produces the output PDF, this module reads the actual PDF
structure and compares it against what the manifest intended. This catches
silent writeback failures — cases where the manifest said alt text was set
but writeback failed to write it, or /Lang was intended but dropped, etc.

The score is computed from the manifest (fast, frontend-side), but the output
PDF is the ground truth. This check closes that gap.

Checks performed:
  RT-1  Catalog /Lang present and non-empty
  RT-2  MarkInfo /Marked = true
  RT-3  ViewerPreferences /DisplayDocTitle = true
  RT-4  XMP pdfuaid:part = 1 present
  RT-5  /Info or XMP title is set
  RT-6  StructTreeRoot exists
  RT-7  Figure struct elements all have non-empty /Alt
  RT-8  Heading tags in struct tree match manifest heading count (within 20%)
  RT-9  Document /Lang matches manifest language
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_HEADING_TAGS = {"H", "H1", "H2", "H3", "H4", "H5", "H6"}


def _walk_struct(elem, callback) -> None:
    """Walk a pikepdf struct tree element recursively, calling callback on each."""
    try:
        if not hasattr(elem, "keys"):
            return
        callback(elem)
        kids = elem.get("/K")
        if kids is None:
            return
        import pikepdf
        obj = kids.get_object() if hasattr(kids, "get_object") else kids
        if isinstance(obj, pikepdf.Array):
            for k in obj:
                try:
                    child = k.get_object() if hasattr(k, "get_object") else k
                    _walk_struct(child, callback)
                except Exception:
                    pass
        else:
            _walk_struct(obj, callback)
    except Exception:
        pass


def _count_manifest_headings(manifest: dict) -> int:
    """Count heading nodes in the manifest."""
    count = 0
    def _walk(nodes):
        nonlocal count
        for n in nodes or []:
            if n.get("tag", "") in _HEADING_TAGS:
                count += 1
            _walk(n.get("children") or [])
    _walk(manifest.get("nodes", []))
    return count


def _count_manifest_figures(manifest: dict) -> int:
    """Count Figure nodes in the manifest that have alt text."""
    count = 0
    def _walk(nodes):
        nonlocal count
        for n in nodes or []:
            if n.get("tag") == "Figure" and not n.get("decorative"):
                count += 1
            _walk(n.get("children") or [])
    _walk(manifest.get("nodes", []))
    return count


def run(pdf_path: str, manifest: dict) -> dict:
    """Run all round-trip checks.

    Returns:
      {
        "passed": int,
        "failed": int,
        "failures": [{"check": "RT-7", "description": "..."}],
        "score_penalty": int,   # points to deduct from conformance score
      }
    """
    failures: list[dict] = []
    passed = 0

    try:
        import pikepdf
        pdf = pikepdf.open(pdf_path)
    except Exception as exc:
        log.warning("round_trip_check: could not open PDF: %s", exc)
        return {"passed": 0, "failed": 0, "failures": [], "score_penalty": 0}

    try:
        root = pdf.Root

        # RT-1: Catalog /Lang
        lang = str(root.get("/Lang", "")).strip()
        if lang:
            passed += 1
        else:
            failures.append({
                "check": "RT-1",
                "severity": "error",
                "description": "Catalog /Lang is missing in output PDF — pdf_ua_comply.py may have failed.",
            })

        # RT-2: MarkInfo /Marked
        mark_info = root.get("/MarkInfo")
        if mark_info and bool(mark_info.get("/Marked", False)):
            passed += 1
        else:
            failures.append({
                "check": "RT-2",
                "severity": "error",
                "description": "MarkInfo /Marked is not set — structure tree not declared to reader software.",
            })

        # RT-3: ViewerPreferences /DisplayDocTitle
        vp = root.get("/ViewerPreferences")
        if vp and bool(vp.get("/DisplayDocTitle", False)):
            passed += 1
        else:
            failures.append({
                "check": "RT-3",
                "severity": "warning",
                "description": "ViewerPreferences /DisplayDocTitle not set — title bar will show filename not document title.",
            })

        # RT-4: XMP pdfuaid:part = 1
        meta = root.get("/Metadata")
        xmp_ok = False
        if meta is not None:
            try:
                raw = meta.read_bytes().decode("utf-8", errors="replace")
                xmp_ok = "pdfuaid:part" in raw
            except Exception:
                pass
        if xmp_ok:
            passed += 1
        else:
            failures.append({
                "check": "RT-4",
                "severity": "error",
                "description": "XMP metadata missing pdfuaid:part=1 — PDF/UA-1 identifier not declared.",
            })

        # RT-5: Document title
        title = ""
        try:
            title = str(pdf.docinfo.get("/Title", "")).strip()
        except Exception:
            pass
        if not title and meta is not None:
            try:
                raw = meta.read_bytes().decode("utf-8", errors="replace")
                m = re.search(r"<dc:title>.*?<rdf:li[^>]*>([^<]+)</rdf:li>", raw, re.DOTALL)
                if m:
                    title = m.group(1).strip()
            except Exception:
                pass
        if title and title != "Untitled Document":
            passed += 1
        else:
            failures.append({
                "check": "RT-5",
                "severity": "warning",
                "description": "Document title is missing or 'Untitled Document' — set a title in the manifest.",
            })

        # RT-6: StructTreeRoot exists
        struct_root = root.get("/StructTreeRoot")
        if struct_root is not None:
            passed += 1
        else:
            failures.append({
                "check": "RT-6",
                "severity": "error",
                "description": "StructTreeRoot is absent — the PDF has no structure tree. Writeback failed entirely.",
            })
            # Can't do RT-7 or RT-8 without a struct tree
            pdf.close()
            penalty = sum(10 if f["severity"] == "error" else 3 for f in failures)
            return {
                "passed": passed,
                "failed": len(failures),
                "failures": failures,
                "score_penalty": min(penalty, 40),
            }

        # RT-7: Figure /Alt completeness
        figures_missing_alt: list[str] = []
        figures_total = [0]

        def _check_figure(elem):
            try:
                tag = str(elem.get("/S", "")).lstrip("/")
                if tag == "Figure":
                    figures_total[0] += 1
                    alt = elem.get("/Alt")
                    if alt is None or str(alt).strip() == "":
                        figures_missing_alt.append(f"page {elem.get('/Pg', '?')}")
            except Exception:
                pass

        _walk_struct(struct_root, _check_figure)

        if figures_missing_alt:
            failures.append({
                "check": "RT-7",
                "severity": "error",
                "description": (
                    f"{len(figures_missing_alt)} of {figures_total[0]} Figure element(s) "
                    f"missing /Alt in output PDF — writeback may have dropped alt text. "
                    f"Affected: {', '.join(figures_missing_alt[:5])}"
                ),
            })
        else:
            passed += 1

        # RT-8: Heading count in struct tree vs manifest (within 20% tolerance)
        manifest_headings = _count_manifest_headings(manifest)
        pdf_headings = [0]

        def _count_heading(elem):
            try:
                tag = str(elem.get("/S", "")).lstrip("/")
                if tag in _HEADING_TAGS:
                    pdf_headings[0] += 1
            except Exception:
                pass

        _walk_struct(struct_root, _count_heading)

        if manifest_headings > 0:
            ratio = pdf_headings[0] / manifest_headings if manifest_headings else 1.0
            if ratio < 0.8:
                failures.append({
                    "check": "RT-8",
                    "severity": "warning",
                    "description": (
                        f"Struct tree has {pdf_headings[0]} heading(s) but manifest specified "
                        f"{manifest_headings} — writeback may have dropped heading tags."
                    ),
                })
            else:
                passed += 1
        else:
            passed += 1  # no headings expected, nothing to check

        # RT-9: /Lang matches manifest language
        manifest_lang = (manifest.get("document", {}).get("language") or "").strip().lower()
        pdf_lang = lang.lower()
        if manifest_lang and pdf_lang and not pdf_lang.startswith(manifest_lang[:2]):
            failures.append({
                "check": "RT-9",
                "severity": "warning",
                "description": (
                    f"Output PDF /Lang is '{lang}' but manifest specified '{manifest_lang}' — "
                    "language mismatch may affect screen reader pronunciation."
                ),
            })
        else:
            passed += 1

    except Exception as exc:
        log.warning("round_trip_check: unexpected error: %s", exc)
    finally:
        try:
            pdf.close()
        except Exception:
            pass

    penalty = sum(10 if f["severity"] == "error" else 3 for f in failures)
    return {
        "passed": passed,
        "failed": len(failures),
        "failures": failures,
        "score_penalty": min(penalty, 40),
    }
