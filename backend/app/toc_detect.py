"""Table of Contents structure detection (PDF/UA clause 7.9).

PDF/UA requires a table of contents to use TOC / TOCI structure elements
rather than plain paragraphs.  This module identifies TOC blocks in the
manifest by pattern-matching:

  - Consecutive P nodes whose text matches the pattern:
      "Some heading title . . . . . 42"
      (title text + ≥ 2 leader chars (dots, dashes, spaces) + page number)
  - A block of ≥ MIN_TOC_ITEMS consecutive such nodes is tagged as:
      TOC (container)  →  TOCI (each entry)

A preceding heading named "Contents", "Table of Contents", or similar is
left as-is (it becomes an H-level element describing the TOC) but the items
immediately following it are wrapped.

The detection is heuristic.  False positives (e.g. lists whose items
coincidentally end with a number) are unlikely but possible in edge cases.
"""

from __future__ import annotations

import re

# Matches a TOC entry:  <title><leader><page_number>
# Title must be ≥ 3 chars; leader ≥ 2 repeating chars; page 1-4 digits.
_TOC_LINE = re.compile(
    r"^(?P<title>.{3,}?)"          # any title text (non-greedy, ≥ 3 chars)
    r"[\s\.·•·\-_]{2,}"           # leader: dots, dashes, spaces (≥ 2)
    r"\s*(?P<page>\d{1,4})\s*$",   # page number at end
    re.UNICODE,
)

# Heading that introduces a TOC block
_TOC_HEADER = re.compile(
    r"^(table\s+of\s+contents?|contents?|toc|outline|index)$",
    re.I,
)

MIN_TOC_ITEMS = 3          # minimum run to call it a TOC
MAX_GAP_PAGES = 1          # allow TOC items to span this many page boundaries


def _node_text(node: dict) -> str:
    return (node.get("text") or "").strip()


def _is_toc_item(node: dict) -> bool:
    text = _node_text(node)
    if not text:
        return False
    return bool(_TOC_LINE.match(text))


def _is_toc_header(node: dict) -> bool:
    return _TOC_HEADER.match(_node_text(node)) is not None


def _make_toc(items: list[dict], base_id: str) -> dict:
    """Wrap a run of TOCI items in a TOC container node."""
    first = items[0]
    return {
        "id": f"{base_id}_toc",
        "tag": "TOC",
        "page": first.get("page"),
        "bbox": first.get("bbox"),
        "source": {"type": "toc_auto"},
        "children": [{**item, "tag": "TOCI"} for item in items],
    }


def detect_toc(manifest: dict) -> dict:
    """Re-tag TOC runs in the top-level manifest nodes list.

    Only processes the top-level ``nodes`` list — nested nodes are left
    unchanged (TOC items are almost always top-level in the manifest).
    Returns the manifest unchanged if no TOC runs are found.
    """
    nodes: list[dict] = manifest.get("nodes", [])
    if not nodes:
        return manifest

    result: list[dict] = []
    i = 0
    changed = 0

    while i < len(nodes):
        node = nodes[i]

        # Case A: node is a TOC header — look ahead for items
        if _is_toc_header(node) and node.get("tag", "P") in ("P", "H1", "H2", "H3", "Caption"):
            # Collect run of TOC items following the header
            j = i + 1
            items: list[dict] = []
            while j < len(nodes):
                candidate = nodes[j]
                if _is_toc_item(candidate):
                    items.append(candidate)
                    j += 1
                elif not _node_text(candidate):
                    j += 1  # skip empty/whitespace-only nodes
                else:
                    break  # non-matching node ends the run

            if len(items) >= MIN_TOC_ITEMS:
                result.append(node)  # keep header as-is
                result.append(_make_toc(items, node["id"]))
                i = j
                changed += len(items)
                continue

        # Case B: node itself is a TOC item (no preceding header)
        if _is_toc_item(node):
            items = [node]
            j = i + 1
            while j < len(nodes):
                candidate = nodes[j]
                if _is_toc_item(candidate):
                    items.append(candidate)
                    j += 1
                elif not _node_text(candidate):
                    j += 1
                else:
                    break

            if len(items) >= MIN_TOC_ITEMS:
                result.append(_make_toc(items, node["id"]))
                i = j
                changed += len(items)
                continue

        result.append(node)
        i += 1

    if not changed:
        return manifest

    src = dict(manifest.get("source") or {})
    src["tocItemsTagged"] = changed
    return {**manifest, "nodes": result, "source": src}
