"""Remediation manifest schema + builder.

The manifest (.tags.json) is the contract between the three stages:

    /autotag   produces a *draft* manifest from OpenDataLoader layout analysis
    studio     lets a human refine it (alt text, reading order, heading levels,
               table headers, title, language)
    /remediate consumes it to write a PDF/UA structure tree back into the file

It is modelled as a **structure tree**: an ordered list of nodes, where
container nodes (Table, TR, TD/TH, L, LI) hold `children`. Reading order is the
pre-order traversal of that tree — i.e. document order — which maps directly
onto the PDF structure hierarchy /remediate will emit.

This module is deliberately free of any web-framework or OpenDataLoader import
so it can be unit-tested and reused by the write-back stage.
"""

from __future__ import annotations

from typing import Any

MANIFEST_VERSION = "1.0"


# --- OpenDataLoader element type -> PDF/UA structure tag -------------------
# OpenDataLoader already emits a `pdfua_tag` for most elements; this map is the
# fallback for elements that don't carry one (e.g. "table row", "caption",
# "list", images), and the canonical source of truth for the studio's tag list.
ODL_TYPE_TO_TAG = {
    "heading": "H1",          # refined to H1..H6 via "heading level"
    "paragraph": "P",
    "table": "Table",
    "table row": "TR",
    "table cell": "TD",       # header cells (TH) are human-flagged — Phase 4
    "list": "L",
    "list item": "LI",
    "caption": "Caption",
    "formula": "Formula",
    "image": "Figure",
    "picture": "Figure",
    "code": "Code",
    "blockquote": "BlockQuote",
}

# Tags that represent images needing alt text or an Artifact decision.
FIGURE_TAGS = {"Figure"}


def _odl(el: dict, *keys: str, default: Any = None) -> Any:
    """Read the first present key from an OpenDataLoader element.

    OpenDataLoader uses space-separated keys ("page number", "bounding box");
    we accept several spellings so the builder is resilient across versions.
    """
    for k in keys:
        if k in el and el[k] is not None:
            return el[k]
    return default


class _IdGen:
    """Stable, document-unique node ids (OpenDataLoader ids are not unique —
    they restart inside table cells)."""

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return f"n{self._n}"


def _tag_for(el: dict) -> str:
    """Resolve the PDF/UA structure tag for an OpenDataLoader element."""
    tag = el.get("pdfua_tag")
    el_type = (el.get("type") or "").lower()

    if el_type == "heading":
        # Prefer an explicit heading level to pick H1..H6.
        level = _odl(el, "heading level", default=1)
        try:
            level = max(1, min(6, int(level)))
        except (TypeError, ValueError):
            level = 1
        return f"H{level}"

    if tag:
        return tag
    return ODL_TYPE_TO_TAG.get(el_type, "P")


def _convert_element(el: dict, ids: _IdGen) -> dict:
    """Convert one OpenDataLoader element (recursively) into a manifest node."""
    el_type = (el.get("type") or "").lower()
    tag = _tag_for(el)

    node: dict[str, Any] = {
        "id": ids.next(),
        "tag": tag,
        "page": _odl(el, "page number", "page", default=1),
        "bbox": _odl(el, "bounding box", "bbox"),
        # Provenance the studio can show / sort by; not written to the PDF.
        "source": {
            "type": el_type,
            "font": el.get("font"),
            "fontSize": _odl(el, "font size"),
        },
    }

    content = el.get("content")
    if content is not None:
        node["text"] = content

    if el_type == "heading":
        node["headingLevel"] = int(str(tag[1:]) or 1)

    if tag in FIGURE_TAGS:
        # Draft defaults — the human supplies alt text or marks decorative.
        node["alt"] = None
        node["decorative"] = False

    children: list[dict] = []

    if el_type == "table":
        node["rows"] = _odl(el, "number of rows")
        node["cols"] = _odl(el, "number of columns")
        for row in _odl(el, "rows", default=[]) or []:
            children.append(_convert_row(row, ids))
    elif el_type == "table cell":
        node["row"] = _odl(el, "row number")
        node["col"] = _odl(el, "column number")
        node["rowSpan"] = _odl(el, "row span", default=1)
        node["colSpan"] = _odl(el, "column span", default=1)
        # Header/scope are human decisions (Phase 4); seed as data cells.
        node["header"] = False
        node["scope"] = None
        for kid in _odl(el, "kids", default=[]) or []:
            children.append(_convert_element(kid, ids))
    else:
        for kid in _odl(el, "kids", default=[]) or []:
            children.append(_convert_element(kid, ids))

    if children:
        node["children"] = children
    return node


def _convert_row(row: dict, ids: _IdGen) -> dict:
    """Convert an OpenDataLoader 'table row' into a TR node with cell children."""
    node: dict[str, Any] = {
        "id": ids.next(),
        "tag": "TR",
        "page": _odl(row, "page number", default=1),
        "row": _odl(row, "row number"),
        "source": {"type": "table row"},
        "children": [_convert_element(cell, ids) for cell in _odl(row, "cells", default=[]) or []],
    }
    return node


def _flat(nodes: list[dict]):
    """Depth-first generator over all manifest nodes (including nested children)."""
    for n in nodes:
        yield n
        yield from _flat(n.get("children") or [])


def _promote_implicit_headings(nodes: list[dict]) -> None:
    """In-place: promote P nodes to H1–H3 when font size indicates heading status.

    This fires only when OpenDataLoader found zero heading elements — meaning the
    document either has no traditional headings or ODL classified them as paragraphs
    (common in non-traditional layouts like video-review docs, slide transcripts,
    and forms).  We use font-size ratios relative to the document body size:

      ratio ≥ 1.5  → H1 (only if no H1 exists)
      ratio ≥ 1.3  → H2
      ratio ≥ 1.15 → H3

    Text longer than 120 characters is never promoted (headings are short).
    """
    HEADING_TAGS = {"H1", "H2", "H3", "H4", "H5", "H6"}
    all_nodes = list(_flat(nodes))

    # Bail if ODL already found headings — trust its detection.
    if any(n.get("tag") in HEADING_TAGS for n in all_nodes):
        return

    # Collect font sizes from P nodes to estimate body size.
    p_nodes = [n for n in all_nodes if n.get("tag") == "P"]
    sizes = sorted(
        sz for n in p_nodes
        if (sz := (n.get("source") or {}).get("fontSize") or 0) > 0
    )
    if len(sizes) < 3:
        return

    # Body size = lower-third median (avoids large-font outliers skewing up).
    body_size = sizes[len(sizes) // 3]
    if body_size <= 0:
        return

    has_h1 = False
    for n in all_nodes:
        if n.get("tag") != "P":
            continue
        sz = (n.get("source") or {}).get("fontSize") or 0
        if sz <= 0:
            continue
        text = (n.get("text") or "").strip()
        if not text or len(text) > 120:
            continue

        ratio = sz / body_size
        if ratio >= 1.5 and not has_h1:
            n["tag"] = "H1"
            n["headingLevel"] = 1
            has_h1 = True
        elif ratio >= 1.3:
            n["tag"] = "H2"
            n["headingLevel"] = 2
        elif ratio >= 1.15:
            n["tag"] = "H3"
            n["headingLevel"] = 3


def build_manifest_from_odl(odl_json: dict, filename: str, engine: str | None = None) -> dict:
    """Build a draft remediation manifest from OpenDataLoader's JSON output."""
    ids = _IdGen()
    nodes = [_convert_element(el, ids) for el in odl_json.get("kids", []) or []]

    # Promote large-font paragraphs to headings when ODL found none.
    _promote_implicit_headings(nodes)

    # OpenDataLoader rarely fills the Info-dict title; surface the heading it
    # flagged as the doc title (level == "Doctitle") as a suggestion instead.
    title = odl_json.get("title")
    suggested_title = None
    # After promotion, check for an H1 to use as the suggested title.
    for n in _flat(nodes):
        if n.get("tag") == "H1" and (n.get("text") or "").strip():
            suggested_title = suggested_title or n["text"].strip()
    for el in odl_json.get("kids", []) or []:
        if (el.get("type") == "heading") and (str(el.get("level", "")).lower() == "doctitle"):
            suggested_title = el.get("content")
            break

    return {
        "version": MANIFEST_VERSION,
        "source": {
            "filename": filename,
            "pageCount": odl_json.get("number of pages"),
            "engine": engine or "opendataloader-pdf",
        },
        "document": {
            # Human-confirmed values; the studio fills these in.
            "title": title,
            "suggestedTitle": suggested_title,
            "language": None,
        },
        "nodes": nodes,
    }


def count_nodes(nodes: list[dict]) -> int:
    """Total node count including nested children (for quick summaries)."""
    total = 0
    for n in nodes:
        total += 1 + count_nodes(n.get("children", []))
    return total
