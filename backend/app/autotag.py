"""OpenDataLoader wrapper -> draft tag manifest.  [Phase 2]

Runs OpenDataLoader (opendataloader-pdf, Apache 2.0) for local layout analysis
and translates its structured JSON (typed blocks with bounding boxes) into the
studio's draft remediation manifest. Falls back to PyMuPDF font-size heuristics
when the ODL Java process is unavailable or crashes.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import tempfile

from .manifest import build_manifest_from_odl
from .tables import analyze_tables

log = logging.getLogger(__name__)


class AutotagError(RuntimeError):
    """OpenDataLoader could not be run, or produced no parseable output."""


# ---------------------------------------------------------------------------
# Invisible-content filtering
# ---------------------------------------------------------------------------

def _invisible_regions(pdf_path: str) -> dict:
    """Return {1-based page: [bbox, ...]} for visually invisible text spans."""
    result: dict = {}
    try:
        import fitz
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
                    if block.get("type") != 0:
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
                                invisible.append((x0, h - y1, x1, h - y0))
            except Exception:
                pass
            if invisible:
                result[page_num] = invisible
    finally:
        doc.close()
    return result


def _bbox_overlap_ratio(a: tuple, b: tuple) -> float:
    """Fraction of bbox *a* covered by bbox *b* (0.0-1.0)."""
    ix0 = max(a[0], b[0]); iy0 = max(a[1], b[1])
    ix1 = min(a[2], b[2]); iy1 = min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max((a[2] - a[0]) * (a[3] - a[1]), 1e-6)
    return inter / area_a


def _filter_invisible_elements(odl_json: dict, invisible: dict) -> dict:
    """Remove ODL leaf elements substantially covered by invisible text regions."""
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
        return "opendataloader-pdf " + version("opendataloader-pdf")
    except Exception:
        return "opendataloader-pdf"


# ---------------------------------------------------------------------------
# PyMuPDF heuristic fallback
# ---------------------------------------------------------------------------

def _heuristic_autotag(pdf_path: str) -> dict:
    """Font-size heuristic manifest using PyMuPDF -- fallback when ODL fails."""
    try:
        import fitz
    except ImportError:
        return {
            "source": {"filename": os.path.basename(pdf_path),
                       "engine": "heuristic-nofitz"},
            "nodes": [],
        }

    nodes: list = []
    n = 0

    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        log.warning("heuristic_autotag: could not open %s: %s", pdf_path, exc)
        return {
            "source": {"filename": os.path.basename(pdf_path),
                       "engine": "heuristic-error"},
            "nodes": [],
        }

    all_sizes: list = []
    pages_blocks: list = []   # (page_num, blocks, page_height_pts)
    try:
        for page_num, page in enumerate(doc, start=1):
            try:
                raw = page.get_text("rawdict", flags=0)
                blocks = raw.get("blocks", []) or []
                page_h = page.rect.height   # PyMuPDF device height for Y-flip
                pages_blocks.append((page_num, blocks, page_h))
                for block in blocks:
                    if block.get("type") != 0:
                        continue
                    for line in block.get("lines", []) or []:
                        for span in line.get("spans", []) or []:
                            sz = span.get("size") or 0
                            if sz > 0:
                                all_sizes.append(sz)
            except Exception:
                pages_blocks.append((page_num, [], 792.0))
    finally:
        doc.close()

    if all_sizes:
        from collections import Counter
        rounded = [round(s * 2) / 2 for s in all_sizes]
        body_size = Counter(rounded).most_common(1)[0][0]
    else:
        body_size = 12.0

    def _line_is_bold_label(line, body_size):
        """Return (is_bold_label, text, avg_size) for a single line."""
        parts, sizes, bold, total = [], [], 0, 0
        for span in line.get("spans", []) or []:
            t = (span.get("text") or "").strip()
            if t:
                parts.append(t)
            sz = span.get("size") or 0
            if sz > 0:
                sizes.append(sz)
            if span.get("flags", 0) & 16:
                bold += 1
            total += 1
        text = " ".join(parts).strip()
        avg = sum(sizes) / len(sizes) if sizes else body_size
        words = len(text.split())
        is_label = (total > 0 and bold / total >= 0.6
                    and words <= 6 and text and not text.endswith("."))
        return is_label, text, avg

    def _classify_tag(avg_size: float, body_size: float, is_label: bool) -> str:
        ratio = avg_size / body_size if body_size else 1.0
        if ratio >= 1.6:
            return "H1"
        if ratio >= 1.3:
            return "H2"
        if ratio >= 1.1:
            return "H3"
        if is_label:
            return "H3"
        return "P"

    for page_num, blocks, page_h in pages_blocks:
        for block in blocks:
            if block.get("type") != 0:
                continue
            lines = block.get("lines", []) or []
            if not lines:
                continue

            # Split block at bold-label lines so "Instruction\nParagraph text..."
            # becomes two nodes: H3("Instruction") + P("Paragraph text...")
            segments: list[tuple[str, list, list]] = []  # (mode, lines_list, ...)
            current_lines: list = []
            current_mode = "body"

            for i, line in enumerate(lines):
                is_label, ltext, lsize = _line_is_bold_label(line, body_size)
                if is_label and current_lines:
                    # flush accumulated body lines
                    segments.append(("body", current_lines))
                    current_lines = []
                if is_label:
                    # emit heading as its own segment immediately
                    segments.append(("label", [line]))
                else:
                    current_lines.append(line)
            if current_lines:
                segments.append(("body", current_lines))

            raw_bbox = block.get("bbox") or [0, 0, 0, 0]
            # Convert PyMuPDF device coords (origin top-left, y↓) to PDF coords
            # (origin bottom-left, y↑) so the manifest is in a single consistent
            # coordinate space that matches ODL output and writeback expectations.
            # PyMuPDF: [x0, y0_top, x1, y1_bottom]  →  PDF: [x0, ph-y1, x1, ph-y0]
            ph = page_h or 792.0
            bbox = [raw_bbox[0], ph - raw_bbox[3], raw_bbox[2], ph - raw_bbox[1]]

            for mode, seg_lines in segments:
                text_parts, sizes, bold_spans, total_spans = [], [], 0, 0
                for line in seg_lines:
                    for span in line.get("spans", []) or []:
                        t = (span.get("text") or "").strip()
                        if t:
                            text_parts.append(t)
                        sz = span.get("size") or 0
                        if sz > 0:
                            sizes.append(sz)
                        if span.get("flags", 0) & 16:
                            bold_spans += 1
                        total_spans += 1
                text = " ".join(text_parts).strip()
                if not text:
                    continue
                avg_size = sum(sizes) / len(sizes) if sizes else body_size
                is_label = mode == "label"
                tag = _classify_tag(avg_size, body_size, is_label)

                n += 1
                nodes.append({
                    "id": "n" + str(n),
                    "tag": tag,
                    "page": page_num,
                    "text": text,
                    "bbox": list(bbox),   # PDF coords (y from bottom)
                    "source": {
                        "type": tag.lower(),
                        "fontSize": avg_size,
                        "engine": "heuristic",
                    },
                })

    return {
        "source": {
            "filename": os.path.basename(pdf_path),
            "engine": "heuristic-pymupdf",
            "bodyFontSize": body_size,
        },
        "nodes": nodes,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def autotag_pdf(pdf_path: str, detect_headers: bool = True) -> dict:
    """Run OpenDataLoader on pdf_path and return a draft manifest dict.

    Falls back to PyMuPDF heuristics if ODL/Java is unavailable or crashes.
    """
    if not os.path.isfile(pdf_path):
        raise AutotagError("PDF not found: " + pdf_path)

    odl_ok = False
    manifest = None

    try:
        import opendataloader_pdf  # type: ignore
    except ImportError:
        log.warning("opendataloader-pdf not installed; using heuristic fallback")
        opendataloader_pdf = None  # type: ignore

    if opendataloader_pdf is not None:
        with tempfile.TemporaryDirectory() as out_dir:
            try:
                opendataloader_pdf.convert(
                    input_path=[pdf_path],
                    output_dir=out_dir,
                    format="json",
                    image_output="off",
                )
                produced = glob.glob(os.path.join(out_dir, "*.json"))
                if produced:
                    with open(produced[0], "r", encoding="utf-8") as fh:
                        odl_json = json.load(fh)
                    invisible = _invisible_regions(pdf_path)
                    if invisible:
                        odl_json = _filter_invisible_elements(odl_json, invisible)
                    manifest = build_manifest_from_odl(
                        odl_json,
                        filename=os.path.basename(pdf_path),
                        engine=_engine_version(),
                    )
                    odl_ok = True
            except Exception as exc:
                log.warning("ODL failed (%s); falling back to heuristic autotag", exc)

    if not odl_ok or manifest is None:
        manifest = _heuristic_autotag(pdf_path)
        manifest.setdefault("source", {})["odlFallback"] = True

    if detect_headers:
        manifest["source"]["tables"] = analyze_tables(manifest)

    node_count = sum(
        1 for n in manifest.get("nodes", []) if n.get("text", "").strip()
    )
    if node_count == 0:
        # --- Claude Vision OCR (primary) ---
        _vision_ok = False
        try:
            from .ocr_vision import detect_scanned_pages, ocr_document, build_manifest_nodes
            scanned = detect_scanned_pages(pdf_path)
            if scanned:
                log.info("autotag: %d scanned page(s) detected — running Vision OCR", len(scanned))
                ocr_doc = ocr_document(pdf_path, scanned)
                vision_nodes = build_manifest_nodes(ocr_doc)
                if vision_nodes:
                    manifest["nodes"] = vision_nodes
                    manifest["_ocr_pages"] = ocr_doc
                    manifest.setdefault("source", {})["ocr"] = "claude-vision"
                    manifest["source"]["scannedPages"] = len(scanned)
                    _vision_ok = True
                    log.info("autotag: Vision OCR produced %d nodes", len(vision_nodes))
        except Exception as _ve:
            log.warning("autotag: Vision OCR failed (%s) — trying Tesseract fallback", _ve)

        # --- Tesseract OCR (fallback) ---
        if not _vision_ok:
            try:
                from .ocr import is_scanned, ocr_pdf
                if is_scanned(pdf_path):
                    ocr_manifest = ocr_pdf(pdf_path)
                    if ocr_manifest.get("nodes"):
                        ocr_manifest["source"]["filename"] = manifest["source"].get("filename", "")
                        return ocr_manifest
            except Exception:
                pass

    from .describe import describe_figures
    manifest = describe_figures(pdf_path, manifest)

    try:
        from .caption_detect import detect_captions
        manifest, _ = detect_captions(manifest)
    except Exception:
        pass

    from .fix_tables import auto_tag_tables
    manifest = auto_tag_tables(manifest)

    try:
        from .table_scope import assign_table_scope
        manifest = assign_table_scope(manifest)
    except Exception:
        pass

    try:
        import pikepdf as _pikepdf
        _pdf_tmp = _pikepdf.open(pdf_path)
        from .radio_group import fix_radio_groups
        manifest, rg_fixed = fix_radio_groups(_pdf_tmp, manifest)
        _pdf_tmp.close()
        manifest.setdefault("source", {})["radioGroupsFixed"] = rg_fixed
    except Exception:
        manifest.setdefault("source", {})["radioGroupsFixed"] = 0

    try:
        from .fix_lists import fix_nested_lists
        manifest = fix_nested_lists(manifest)
    except Exception:
        pass

    try:
        from .toc_detect import detect_toc
        manifest = detect_toc(manifest)
    except Exception:
        pass

    try:
        from .header_footer import detect_header_footer_zones, mark_header_footer_nodes
        zones = detect_header_footer_zones(pdf_path)
        artifact_count = mark_header_footer_nodes(manifest, zones)
        manifest["source"]["headerFooterArtifacts"] = artifact_count
    except Exception:
        manifest["source"]["headerFooterArtifacts"] = 0

    try:
        from .reading_order import fix_reading_order
        manifest, order_changes = fix_reading_order(manifest)
        manifest["source"]["readingOrderFixed"] = order_changes
    except Exception:
        manifest["source"]["readingOrderFixed"] = 0

    try:
        from .lang_detect import detect_node_languages
        manifest, lang_changes = detect_node_languages(manifest)
        manifest["source"]["langAnnotations"] = lang_changes
    except Exception:
        manifest["source"]["langAnnotations"] = 0

    try:
        from .formula_tag import tag_formulas
        manifest, _ = tag_formulas(manifest)
    except Exception:
        pass

    return manifest
