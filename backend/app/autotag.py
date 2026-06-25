"""OpenDataLoader wrapper -> draft tag manifest.  [Phase 2]

Runs OpenDataLoader (opendataloader-pdf, Apache 2.0) for local layout analysis
and translates its structured JSON (typed blocks with bounding boxes) into the
studio's draft remediation manifest. This replaces font-size heuristics with
real layout analysis, and the bounding boxes are what make Phase 3 write-back
tractable (binding each node to the marked-content sequence on its page).

OpenDataLoader is a Java tool with a thin Python wrapper; it needs Java 11+,
which the Docker image provides (Temurin 11 at $JAVA_HOME).
"""

from __future__ import annotations

import glob
import json
import os
import tempfile

from .manifest import build_manifest_from_odl
from .tables import analyze_tables


class AutotagError(RuntimeError):
    """OpenDataLoader could not be run, or produced no parseable output."""


# ---------------------------------------------------------------------------
# Invisible-content filtering
# ---------------------------------------------------------------------------

def _invisible_regions(pdf_path: str) -> dict[int, list[tuple[float, float, float, float]]]:
    """Return {1-based page → [bbox, …]} for text that is visually invisible.

    Invisible spans include:
    - Text rendered in white or near-white (fills background, conveys nothing)
    - Text with font size < 2pt (sub-pixel; used for hidden OCR/search layers)

    Bboxes are in PDF user-space (origin bottom-left, y increases upward) so
    they match the coordinate space OpenDataLoader uses.  We convert from
    PyMuPDF's top-left origin by flipping y: y_pdf = page_height - y_mupdf.
    """
    result: dict[int, list] = {}
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return result
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return result
    try:
        for page_num, page in enumerate(doc, start=1):
            h = page.rect.height
            invisible: list = []
            try:
                raw = page.get_text("rawdict", flags=0)
                for block in raw.get("blocks", []) or []:
                    if block.get("type") != 0:  # 0 = text block
                        continue
                    for line in block.get("lines", []) or []:
                        for span in line.get("spans", []) or []:
                            size  = span.get("size", 12) or 0
                            color = span.get("color", 0) or 0
                            r = (color >> 16) & 0xFF
                            g = (color >> 8)  & 0xFF
                            b =  color        & 0xFF
                            is_white = r > 245 and g > 245 and b > 245
                            is_tiny  = 0 < size < 2
                            if is_white or is_tiny:
                                x0, y0, x1, y1 = span["bbox"]
                                # Convert MuPDF (top-left origin) → PDF space
                                invisible.append((x0, h - y1, x1, h - y0))
            except Exception:
                pass
            if invisible:
                result[page_num] = invisible
    finally:
        doc.close()
    return result


def _bbox_overlap_ratio(a: tuple, b: tuple) -> float:
    """Fraction of bbox *a* that is covered by bbox *b* (0.0–1.0)."""
    ix0 = max(a[0], b[0]); iy0 = max(a[1], b[1])
    ix1 = min(a[2], b[2]); iy1 = min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max((a[2] - a[0]) * (a[3] - a[1]), 1e-6)
    return inter / area_a


def _filter_invisible_elements(odl_json: dict, invisible: dict) -> dict:
    """Remove ODL elements that are substantially covered by invisible text regions.

    An element is dropped when ≥ 60 % of its bounding box overlaps with
    invisible spans on the same page.  Containers (Table, list) are not
    dropped — only leaf text elements (paragraph, heading, etc.).
    """
    if not invisible:
        return odl_json

    LEAF_TYPES = {"paragraph", "heading", "caption", "formula", "code", "blockquote"}

    def _should_drop(el: dict) -> bool:
        el_type = (el.get("type") or "").lower()
        if el_type not in LEAF_TYPES:
            return False
        page  = el.get("page number") or el.get("page") or 1
        bbox  = el.get("bounding box") or el.get("bbox")
        if not bbox or len(bbox) < 4:
            return False
        regions = invisible.get(page, [])
        for region in regions:
            if _bbox_overlap_ratio(tuple(bbox), region) >= 0.6:
                return True
        return False

    def _clean(elements: list) -> list:
        kept = []
        for el in elements or []:
            if _should_drop(el):
                continue
            # Recurse into kids
            kids = el.get("kids")
            if kids:
                el = dict(el)
                el["kids"] = _clean(kids)
            rows = el.get("rows")
            if rows:
                el = dict(el)
                el["rows"] = [
                    {**r, "cells": _clean(r.get("cells", []))}
                    for r in rows
                ]
            kept.append(el)
        return kept

    cleaned = dict(odl_json)
    cleaned["kids"] = _clean(odl_json.get("kids", []))
    return cleaned


def _engine_version() -> str:
    try:
        from importlib.metadata import version

        return f"opendataloader-pdf {version('opendataloader-pdf')}"
    except Exception:  # pragma: no cover - defensive
        return "opendataloader-pdf"


def autotag_pdf(pdf_path: str, detect_headers: bool = True) -> dict:
    """Run OpenDataLoader on ``pdf_path`` and return a draft manifest dict.

    When ``detect_headers`` is set (default), table header cells are proposed
    (first row -> column headers) so the studio arrives pre-filled; the human
    confirms or overrides. Raises ``AutotagError`` on any plumbing failure.
    """
    if not os.path.isfile(pdf_path):
        raise AutotagError(f"PDF not found: {pdf_path}")

    try:
        import opendataloader_pdf  # type: ignore
    except ImportError as exc:  # pragma: no cover - image always has it
        raise AutotagError(
            "opendataloader-pdf is not installed in this environment."
        ) from exc

    with tempfile.TemporaryDirectory() as out_dir:
        try:
            # JSON only; don't extract image pixel data (the studio renders the
            # page itself and only needs each figure's bounding box).
            opendataloader_pdf.convert(
                input_path=[pdf_path],
                output_dir=out_dir,
                format="json",
                image_output="off",
            )
        except Exception as exc:
            raise AutotagError(f"OpenDataLoader failed: {exc}") from exc

        produced = glob.glob(os.path.join(out_dir, "*.json"))
        if not produced:
            raise AutotagError(
                "OpenDataLoader produced no JSON output (is Java 11+ available?)."
            )

        with open(produced[0], "r", encoding="utf-8") as fh:
            odl_json = json.load(fh)

    # Strip elements whose bboxes lie entirely within invisible text regions
    # (OCR search layers, white-on-white text, sub-pixel font overlays).
    invisible = _invisible_regions(pdf_path)
    if invisible:
        odl_json = _filter_invisible_elements(odl_json, invisible)

    manifest = build_manifest_from_odl(
        odl_json,
        filename=os.path.basename(pdf_path),
        engine=_engine_version(),
    )

    if detect_headers:
        manifest["source"]["tables"] = analyze_tables(manifest)

    # If ODL found no text nodes the PDF is likely a scanned image; try OCR.
    node_count = sum(
        1 for n in manifest.get("nodes", []) if n.get("text", "").strip()
    )
    if node_count == 0:
        try:
            from .ocr import is_scanned, ocr_pdf
            if is_scanned(pdf_path):
                ocr_manifest = ocr_pdf(pdf_path)
                if ocr_manifest.get("nodes"):
                    ocr_manifest["source"]["filename"] = manifest["source"].get("filename", "")
                    return ocr_manifest
        except Exception:
            pass  # OCR unavailable — return original (empty) manifest

    from .describe import describe_figures
    manifest = describe_figures(pdf_path, manifest)

    # Associate figure captions — Sprint 19 (PDF/UA 7.3).
    try:
        from .caption_detect import detect_captions
        manifest, _ = detect_captions(manifest)
    except Exception:
        pass

    from .fix_tables import auto_tag_tables
    manifest = auto_tag_tables(manifest)

    # Assign TH /Scope attributes (Column/Row/Both) — Sprint 15.
    try:
        from .table_scope import assign_table_scope
        manifest = assign_table_scope(manifest)
    except Exception:
        pass

    # Radio button group consolidation — Sprint 16.
    # Runs before writeback so skip_struct flags affect struct tree building.
    try:
        import pikepdf as _pikepdf
        _pdf_tmp = _pikepdf.open(pdf_path)
        from .radio_group import fix_radio_groups
        manifest, rg_fixed = fix_radio_groups(_pdf_tmp, manifest)
        _pdf_tmp.close()
        manifest.setdefault("source", {})["radioGroupsFixed"] = rg_fixed
    except Exception:
        manifest.setdefault("source", {})["radioGroupsFixed"] = 0

    # Detect and repair nested list structure (WCAG 1.3.1 / PDF/UA 7.7).
    try:
        from .fix_lists import fix_nested_lists
        manifest = fix_nested_lists(manifest)
    except Exception:
        pass

    # Detect TOC blocks and re-tag as TOC/TOCI (PDF/UA clause 7.9).
    try:
        from .toc_detect import detect_toc
        manifest = detect_toc(manifest)
    except Exception:
        pass

    # Detect running headers/footers and mark them as artifacts so write-back
    # routes them to /Artifact marked content (WCAG: skip page furniture).
    try:
        from .header_footer import detect_header_footer_zones, mark_header_footer_nodes
        zones = detect_header_footer_zones(pdf_path)
        artifact_count = mark_header_footer_nodes(manifest, zones)
        manifest["source"]["headerFooterArtifacts"] = artifact_count
    except Exception:
        manifest["source"]["headerFooterArtifacts"] = 0

    # Fix reading order — sort nodes by bounding box position (top-to-bottom,
    # left-to-right), with column detection for multi-column layouts.
    # WCAG 1.3.2: Meaningful sequence.
    try:
        from .reading_order import fix_reading_order
        manifest, order_changes = fix_reading_order(manifest)
        manifest["source"]["readingOrderFixed"] = order_changes
    except Exception:
        manifest["source"]["readingOrderFixed"] = 0

    # Per-section language detection — WCAG 3.1.2: Language of Parts.
    try:
        from .lang_detect import detect_node_languages
        manifest, lang_changes = detect_node_languages(manifest)
        manifest["source"]["langAnnotations"] = lang_changes
    except Exception:
        manifest["source"]["langAnnotations"] = 0

    # Detect and repair nested list structure (WCAG 1.3.1 / PDF/UA 7.7).
    try:
        from .fix_lists import fix_nested_lists
        manifest = fix_nested_lists(manifest)
    except Exception:
        pass

    # Detect TOC blocks and re-tag as TOC/TOCI (PDF/UA clause 7.9).
    try:
        from .toc_detect import detect_toc
        manifest = detect_toc(manifest)
    except Exception:
        pass

    

    # Formula / math expression detection — Sprint 21 (PDF/UA 7.8).
    try:
        from .formula_tag import tag_formulas
        manifest, _ = tag_formulas(manifest)
    except Exception:
        pass
