"""Detect running headers and footers in a PDF using PyMuPDF.

Pages commonly have department names, document titles, page numbers, and
date stamps in the top or bottom margins. These repeat across multiple pages
at the same position and are not document content — they should be marked
/Artifact in the structure tree so screen readers skip them.

Algorithm:
  1. For each page, gather text spans in the top MARGIN_FRACTION or bottom
     MARGIN_FRACTION of the page height.
  2. Group by approximate y-position (within Y_TOLERANCE) and normalized text
     (digits replaced with #, so "Page 3" and "Page 7" match).
  3. Spans that appear on MIN_PAGES or more pages at the same y are candidates.
  4. Return a set of (page_1indexed, bbox_tuple) pairs.  The autotag stage
     marks manifest nodes whose bbox center overlaps one of these pairs as
     artifact=True; writeback then routes them to /Artifact marked content.
"""

from __future__ import annotations

import re
from collections import defaultdict

MARGIN_FRACTION = 0.10   # top/bottom 10% of page height
Y_TOLERANCE = 6.0        # pt — groups y-positions that are "the same row"
MIN_PAGES = 2            # minimum page appearances to call it a running element


def detect_header_footer_zones(pdf_path: str) -> set[tuple[int, tuple]]:
    """Return (page_number_1indexed, bbox_tuple) pairs that are headers/footers.

    Returns an empty set if PyMuPDF is unavailable or the PDF has fewer than
    MIN_PAGES pages (single-page docs have no "running" elements by definition).
    """
    try:
        import fitz
    except ImportError:
        return set()

    doc = fitz.open(pdf_path)
    try:
        if len(doc) < MIN_PAGES:
            return set()

        groups: dict[tuple, list[tuple[int, tuple]]] = defaultdict(list)

        for page_num, page in enumerate(doc):
            rect = page.rect
            h = rect.height
            top_thresh = h * MARGIN_FRACTION
            bot_thresh = h * (1.0 - MARGIN_FRACTION)

            blocks = page.get_text("dict", flags=0).get("blocks", [])
            for block in blocks:
                if block.get("type") != 0:   # 0 = text
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = span.get("text", "").strip()
                        if not text:
                            continue
                        bbox = span.get("bbox", (0, 0, 0, 0))
                        y_mid = (bbox[1] + bbox[3]) / 2.0

                        if y_mid < top_thresh:
                            zone = "top"
                        elif y_mid > bot_thresh:
                            zone = "bottom"
                        else:
                            continue

                        # Normalise: collapse whitespace, replace digits with #
                        normalized = re.sub(r"\d+", "#", text).strip() or "#"
                        rounded_y = round(y_mid / Y_TOLERANCE) * Y_TOLERANCE
                        key = (zone, rounded_y, normalized)
                        groups[key].append((page_num + 1, tuple(bbox)))

        artifact_zones: set[tuple[int, tuple]] = set()
        for key, occurrences in groups.items():
            if len(occurrences) >= MIN_PAGES:
                for item in occurrences:
                    artifact_zones.add(item)

        return artifact_zones
    finally:
        doc.close()


def _iter_nodes(nodes: list):
    """Yield every node in the manifest tree, depth-first."""
    for node in nodes:
        yield node
        yield from _iter_nodes(node.get("children", []) or [])


def mark_header_footer_nodes(manifest: dict, artifact_zones: set) -> int:
    """Set artifact=True on manifest nodes that overlap a detected zone.

    Returns the count of nodes marked.
    """
    if not artifact_zones:
        return 0

    page_artifacts: dict[int, list[tuple]] = defaultdict(list)
    for page_num, bbox in artifact_zones:
        page_artifacts[page_num].append(bbox)

    count = 0
    TOL = 4.0
    for node in _iter_nodes(manifest.get("nodes", [])):
        page = int(node.get("page", 0))
        bbox = node.get("bbox")
        if not bbox or page not in page_artifacts:
            continue
        nx = (bbox[0] + bbox[2]) / 2.0
        ny = (bbox[1] + bbox[3]) / 2.0
        for az in page_artifacts[page]:
            if (az[0] - TOL <= nx <= az[2] + TOL and
                    az[1] - TOL <= ny <= az[3] + TOL):
                node["artifact"] = True
                count += 1
                break

    return count
