"""Figure caption detection and association — Sprint 19 (PDF/UA 7.3).

Scans the manifest for Figure nodes and tries to associate nearby text
elements that look like captions ("Figure 1:", "Fig.", numbered patterns)
as Caption children of the Figure struct element.

PDF/UA-1 clause 7.3: figures must have /Alt; captions should be Caption
struct elements nested inside the Figure.
"""

from __future__ import annotations

import re

_CAPTION_RE = re.compile(
    r"^(fig(?:ure)?\.?\s*\d|table\s*\d|exhibit\s*\d|chart\s*\d|"
    r"image\s*\d|photo\s*\d|plate\s*\d|diagram\s*\d)",
    re.IGNORECASE,
)
_MAX_GAP_PTS = 40  # max vertical gap (PDF pts) between figure and caption


def _vertical_gap(fig_bbox: list, txt_bbox: list) -> float:
    """Vertical gap between a figure bbox and a text bbox (PDF space, y-up)."""
    if not fig_bbox or not txt_bbox or len(fig_bbox) < 4 or len(txt_bbox) < 4:
        return float("inf")
    fig_y0 = min(fig_bbox[1], fig_bbox[3])
    fig_y1 = max(fig_bbox[1], fig_bbox[3])
    txt_y0 = min(txt_bbox[1], txt_bbox[3])
    txt_y1 = max(txt_bbox[1], txt_bbox[3])
    gap_below = abs(fig_y0 - txt_y1)   # caption below figure
    gap_above = abs(txt_y0 - fig_y1)   # caption above figure
    return min(gap_below, gap_above)


def detect_captions(manifest: dict) -> tuple[dict, int]:
    """Associate caption text nodes with their nearest Figure parent.

    Returns (updated_manifest, captions_linked_count).
    """
    nodes: list = manifest.get("nodes", [])
    if not nodes:
        return manifest, 0

    figures: list[tuple[int, dict]] = []
    candidates: list[tuple[int, dict]] = []

    for i, node in enumerate(nodes):
        tag = node.get("tag", "")
        text = (node.get("text") or "").strip()
        if tag == "Figure":
            figures.append((i, node))
        elif tag in ("P", "Span", "Caption") and _CAPTION_RE.match(text):
            candidates.append((i, node))

    if not figures or not candidates:
        return manifest, 0

    linked = 0
    consumed: set[int] = set()

    for fig_idx, fig_node in figures:
        fig_bbox = fig_node.get("bbox")
        fig_page = fig_node.get("page", 1)
        best_gap, best_ci, best_cand = float("inf"), None, None

        for ci, cand in candidates:
            if ci in consumed:
                continue
            if cand.get("page", 1) != fig_page:
                continue
            gap = _vertical_gap(fig_bbox, cand.get("bbox"))
            if gap < best_gap:
                best_gap, best_ci, best_cand = gap, ci, cand

        if best_cand is not None and best_gap <= _MAX_GAP_PTS:
            cap = {**best_cand, "tag": "Caption"}
            children = list(fig_node.get("children", []))
            children.insert(0, cap)
            nodes[fig_idx] = {**fig_node, "children": children}
            consumed.add(best_ci)
            linked += 1

    manifest["nodes"] = [n for i, n in enumerate(nodes) if i not in consumed]
    manifest.setdefault("source", {})["captionsLinked"] = linked
    return manifest, linked
