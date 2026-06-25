"""AI-driven layout analysis pass (WCAG Sprint 6).

Runs after OpenDataLoader to make decisions that require visual understanding
of the page — things no bbox heuristic can reliably determine:

  • Reading order for complex layouts (sidebars, callouts, pull quotes)
  • Table header identification: which cells are headers, with correct scope
    (row / col / both / rowgroup / colgroup) for multi-level tables
  • Mathematical formula alt text (natural language + LaTeX)
  • Caption → Figure grouping (Caption becomes child of Figure in struct tree)
  • Layout table detection (visual-layout tables tagged as Artifact)
  • List item Lbl/LBody splitting (bullet/number marker separated from body)

Uses Claude vision (claude-haiku-4-5-20251001) — one call per page for
layout decisions, separate region crops for tables, formulas, and list items.

Every step degrades gracefully: a failure on one page/table/formula never
blocks the rest of the analysis, and the module returns the original manifest
unchanged when ANTHROPIC_API_KEY is not set.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

MODEL         = "claude-haiku-4-5-20251001"
MAX_TOKENS    = 1024
PAGE_SCALE    = 1.0      # render scale — we cap by MAX_PAGE_DIM instead
REGION_SCALE  = 2.0      # higher res for region crops (tables, formulas)
MAX_PAGE_DIM  = 1024     # longest edge in pixels for page-level images
MAX_PAGES     = 25       # skip page-level analysis beyond this


# ── Rendering helpers ─────────────────────────────────────────────────────────

def _png_b64(img_bytes: bytes) -> str:
    return base64.standard_b64encode(img_bytes).decode()


def _render_page(doc, page_idx: int, max_dim: int = MAX_PAGE_DIM) -> bytes | None:
    """Render a full page scaled so its longest edge ≤ max_dim."""
    try:
        import fitz
        page = doc[page_idx]
        pw, ph = page.rect.width, page.rect.height
        scale = min(max_dim / max(pw, ph, 1), 2.0)
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
        return pix.tobytes("png")
    except Exception as exc:
        log.debug("render_page failed page=%d: %s", page_idx, exc)
        return None


def _render_region(doc, page_idx: int, bbox: list, scale: float = REGION_SCALE) -> bytes | None:
    """Render a bounding box [left, bottom, right, top] in PDF coords."""
    try:
        import fitz
        page = doc[page_idx]
        h = page.rect.height
        l, b, r, t = bbox
        rect = fitz.Rect(l, h - t, r, h - b)
        if rect.is_empty or rect.width < 4 or rect.height < 4:
            return None
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, clip=rect, colorspace=fitz.csRGB, alpha=False)
        return pix.tobytes("png")
    except Exception as exc:
        log.debug("render_region failed page=%d bbox=%s: %s", page_idx, bbox, exc)
        return None


# ── Claude call wrapper ───────────────────────────────────────────────────────

def _call(client, prompt: str, img_bytes: bytes) -> str | None:
    """Single Claude vision call → raw text, or None on error."""
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": _png_b64(img_bytes),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return resp.content[0].text.strip()
    except Exception as exc:
        log.debug("Claude call failed: %s", exc)
        return None


def _parse_json(text: str) -> dict | list | None:
    """Parse JSON from AI response, tolerating surrounding prose."""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r'\{[\s\S]*\}|\[[\s\S]*\]', text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return None


# ── Manifest node helpers ─────────────────────────────────────────────────────

def _text_of(node: dict, max_len: int = 60) -> str:
    """Short text preview for a manifest node."""
    t = (node.get("text") or node.get("alt") or "").strip()
    if not t:
        for child in (node.get("children") or []):
            t = _text_of(child, max_len)
            if t:
                break
    return t[:max_len]


def _flat(nodes: list[dict]):
    """Depth-first generator over all manifest nodes."""
    for n in nodes:
        yield n
        yield from _flat(n.get("children") or [])


def _page_nodes(nodes: list[dict], page: int) -> list[dict]:
    """Top-level nodes on 1-based page number."""
    return [n for n in nodes if n.get("page") == page]


def _patch(nodes: list[dict], node_id: str, patch: dict) -> list[dict]:
    """Return new nodes list with the named node patched (recursive)."""
    out = []
    for n in nodes:
        if n.get("id") == node_id:
            n = {**n, **patch}
        if n.get("children"):
            n = {**n, "children": _patch(n["children"], node_id, patch)}
        out.append(n)
    return out


# ── 1. Page-level layout analysis ─────────────────────────────────────────────

_PAGE_PROMPT = """You are remediating a PDF for WCAG 2.2 AA accessibility.

I have provided a rendered image of one page and the content elements detected on it.
Each element has: id, tag (PDF/UA struct type), text preview, bbox [left, bottom, right, top].

Return a single JSON object. Omit any key where you have no correction to make.

{
  "reading_order": ["id1", "id2", ...],
  "layout_tables": ["id1", ...],
  "caption_figure_pairs": [{"caption": "id1", "figure": "id2"}, ...],
  "footnote_pairs": [{"ref_id": "id_of_superscript_node", "note_id": "id_of_footnote_body_node"}, ...]
}

RULES:
- reading_order: list ALL provided IDs exactly once in correct reading sequence.
  Multi-column: finish the left column completely before starting the right.
  Sidebars / callout boxes / pull quotes: place them AFTER the paragraph that introduces them,
  not at their visual position.
- layout_tables: IDs of Table elements used purely for page layout (e.g. a two-column text
  grid, a header/footer cell), not for data. These will be hidden from screen readers.
- caption_figure_pairs: when a Caption element describes a specific nearby Figure, pair them
  so the Caption becomes a child of the Figure in the structure tree.
- footnote_pairs: if you can match superscript footnote markers in body text to numbered
  footnote text at the bottom of the page, list the pairs by node ID.
  Only include pairs where you are confident of the match.

Detected elements:
{elements}

Return ONLY valid JSON. No explanation or markdown."""


def _analyze_page(client, img: bytes, page_nodes: list[dict]) -> dict:
    if not page_nodes or not img:
        return {}
    elements = [
        {"id": n["id"], "tag": n.get("tag","P"), "text": _text_of(n), "bbox": n.get("bbox") or []}
        for n in page_nodes
    ]
    raw = _call(client, _PAGE_PROMPT.format(elements=json.dumps(elements, indent=2)), img)
    result = _parse_json(raw or "")
    return result if isinstance(result, dict) else {}


def _apply_reading_order(nodes: list[dict], page: int, ordered_ids: list[str]) -> list[dict]:
    if not ordered_ids:
        return nodes
    id_map = {n["id"]: n for n in nodes if n.get("page") == page}
    reordered = [id_map[i] for i in ordered_ids if i in id_map]
    # Add any page nodes AI omitted (safety net)
    seen = set(ordered_ids)
    reordered += [n for n in nodes if n.get("page") == page and n["id"] not in seen]
    # Find insertion point: position of first page node in original list
    first_idx = next((i for i, n in enumerate(nodes) if n.get("page") == page), None)
    if first_idx is None:
        return nodes
    non_page = [n for n in nodes if n.get("page") != page]
    before = [n for n in nodes[:first_idx] if n.get("page") != page]
    after  = [n for n in nodes[first_idx:] if n.get("page") != page]
    return before + reordered + after


def _apply_caption_figure_pairs(nodes: list[dict], pairs: list[dict]) -> list[dict]:
    if not pairs:
        return nodes
    all_flat = {n["id"]: n for n in _flat(nodes)}
    absorb   = {p["caption"]: p["figure"] for p in pairs if "caption" in p and "figure" in p}

    def _walk(ns: list[dict]) -> list[dict]:
        out = []
        for n in ns:
            if n["id"] in absorb:
                continue  # will be inserted as child of its figure
            children = list(n.get("children") or [])
            # If this is the figure, append its caption as last child
            caption_id = next((c for c, f in absorb.items() if f == n["id"]), None)
            if caption_id:
                cap = all_flat.get(caption_id)
                if cap and not any(c["id"] == caption_id for c in children):
                    children.append(cap)
            n = {**n, "children": _walk(children)} if (children or n.get("children")) else n
            out.append(n)
        return out

    return _walk(nodes)


# ── 2. Table header analysis ──────────────────────────────────────────────────

_TABLE_PROMPT = """You are identifying table headers in a PDF for accessibility.

The table has {rows} rows and {cols} columns. Current cells:
{cells}

Return a JSON object:
{
  "is_layout_table": false,
  "cells": [
    {"row": 0, "col": 0, "is_header": true, "scope": "col"},
    ...
  ]
}

"is_layout_table": true only if this table is used purely for visual page layout (no data).
"scope" — use EXACTLY one of: "col" (column header), "row" (row header),
  "both" (corner cell where row and column headers intersect),
  "rowgroup" (header spanning a group of rows), "colgroup" (header spanning a group of columns).
Only list cells where is_header is true. You may omit cells that are plain data cells.
Return ONLY valid JSON."""


def _table_cells(table: dict) -> list[dict]:
    out = []
    for tr in (table.get("children") or []):
        if tr.get("tag") != "TR":
            continue
        for cell in (tr.get("children") or []):
            if cell.get("tag") not in ("TD","TH"):
                continue
            out.append({
                "id": cell["id"],
                "row": cell.get("row", 0),
                "col": cell.get("col", 0),
                "rowSpan": cell.get("rowSpan", 1),
                "colSpan": cell.get("colSpan", 1),
                "text": _text_of(cell),
                "currently_header": cell.get("header", False),
            })
    return out


def _analyze_table(client, doc, table: dict, page_idx: int) -> dict:
    bbox = table.get("bbox")
    if not bbox:
        return {}
    img = _render_region(doc, page_idx, bbox)
    if not img:
        return {}
    cells = _table_cells(table)
    if not cells:
        return {}
    rows = table.get("rows") or (max(c["row"] for c in cells) + 1)
    cols = table.get("cols") or (max(c["col"] for c in cells) + 1)
    # Strip internal IDs from what we send (AI doesn't need them)
    cells_for_prompt = [{k:v for k,v in c.items() if k != "id"} for c in cells]
    raw = _call(client, _TABLE_PROMPT.format(
        rows=rows, cols=cols,
        cells=json.dumps(cells_for_prompt, indent=2),
    ), img)
    result = _parse_json(raw or "")
    return result if isinstance(result, dict) else {}


def _apply_table_analysis(nodes: list[dict], table_id: str, analysis: dict) -> list[dict]:
    if not analysis:
        return nodes
    if analysis.get("is_layout_table"):
        return _patch(nodes, table_id, {"tag": "Artifact", "decorative": True})

    # Build (row, col) → decision map
    decisions = {(c["row"], c["col"]): c for c in (analysis.get("cells") or [])}
    if not decisions:
        return nodes

    def _walk(ns: list[dict]) -> list[dict]:
        out = []
        for n in ns:
            if n.get("id") == table_id:
                new_children = []
                for tr in (n.get("children") or []):
                    new_cells = []
                    for cell in (tr.get("children") or []):
                        key = (cell.get("row", 0), cell.get("col", 0))
                        if key in decisions:
                            d = decisions[key]
                            p: dict = {}
                            if d.get("is_header"):
                                p["header"] = True
                                p["tag"] = "TH"
                            if d.get("scope"):
                                p["scope"] = d["scope"]
                            cell = {**cell, **p}
                        new_cells.append(cell)
                    new_children.append({**tr, "children": new_cells})
                n = {**n, "children": new_children}
            elif n.get("children"):
                n = {**n, "children": _walk(n["children"])}
            out.append(n)
        return out

    return _walk(nodes)


# ── 3. Formula / math alt text ────────────────────────────────────────────────

_FORMULA_PROMPT = """You are generating alt text for a content element in an accessible PDF.

Classify this image and return a JSON object (return ONLY valid JSON):

If it is a MATHEMATICAL expression, equation, formula, or scientific notation:
{"is_math": true, "alt_text": "natural language read-aloud (e.g. 'x squared plus y squared equals r squared')", "latex": "LaTeX string or empty string if not discernible"}

If it is DECORATIVE (divider line, border, watermark, background graphic):
{"decorative": true, "alt_text": ""}

Otherwise describe it:
{"is_math": false, "alt_text": "1-2 sentence description of what this image communicates"}"""


def _analyze_formula(client, doc, node: dict, page_idx: int) -> dict:
    bbox = node.get("bbox")
    if not bbox:
        return {}
    img = _render_region(doc, page_idx, bbox)
    if not img:
        return {}
    raw = _call(client, _FORMULA_PROMPT, img)
    result = _parse_json(raw or "")
    return result if isinstance(result, dict) else {}


# ── 4. List item Lbl / LBody split ───────────────────────────────────────────

_LIST_PROMPT = """You are splitting a PDF list item into its marker and content for accessibility.

A list item has two structural parts:
  • Lbl  — the bullet symbol, number, or letter marker (e.g. "•", "1.", "a)", "–")
  • LBody — the actual content text

Look at this list item image and return:
{"label": "exact marker text", "body_preview": "first few words of body"}

If there is no distinct marker (e.g. a bare paragraph in a list):
{"label": null, "body_preview": "full text preview"}

Return ONLY valid JSON."""


def _analyze_list_item(client, doc, node: dict, page_idx: int) -> dict:
    bbox = node.get("bbox")
    if not bbox:
        return {}
    img = _render_region(doc, page_idx, bbox)
    if not img:
        return {}
    raw = _call(client, _LIST_PROMPT, img)
    result = _parse_json(raw or "")
    return result if isinstance(result, dict) else {}


def _apply_lbl_lbody(nodes: list[dict], li_id: str, label: str) -> list[dict]:
    """Wrap LI children in Lbl + LBody sub-elements."""
    def _walk(ns: list[dict]) -> list[dict]:
        out = []
        for n in ns:
            if n.get("id") == li_id and n.get("tag") == "LI":
                if not n.get("children"):
                    text = n.get("text", "")
                    body_text = text[len(label):].lstrip() if text.startswith(label) else text
                    lbl  = {"id": f"{li_id}_lbl",   "tag": "Lbl",   "text": label,     "page": n.get("page"), "bbox": n.get("bbox"), "source": {"type": "lbl"}}
                    lbody= {"id": f"{li_id}_lbody", "tag": "LBody", "text": body_text,  "page": n.get("page"), "bbox": n.get("bbox"), "source": {"type": "lbody"}}
                    n = {**n, "children": [lbl, lbody]}
            elif n.get("children"):
                n = {**n, "children": _walk(n["children"])}
            out.append(n)
        return out
    return _walk(nodes)


# ── Main entry point ───────────────────────────────────────────────────────────

def analyze_pdf(pdf_path: str, manifest: dict) -> dict:
    """Run all AI layout analysis passes and return an enriched manifest.

    Safe to call unconditionally — returns manifest unchanged when:
      • ANTHROPIC_API_KEY is not set
      • PyMuPDF / anthropic are not installed
      • Any individual step fails (each degrades independently)
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return manifest

    nodes = manifest.get("nodes", [])
    if not nodes:
        return manifest

    try:
        import anthropic
        import fitz  # noqa: F401 — just checking import
    except ImportError:
        return manifest

    client = anthropic.Anthropic(api_key=api_key)

    stats: dict[str, int] = {
        "pages_analyzed": 0,
        "reading_order_corrections": 0,
        "layout_tables_detected": 0,
        "captions_grouped": 0,
        "footnote_pairs": 0,
        "tables_analyzed": 0,
        "header_cells_updated": 0,
        "formulas_described": 0,
        "list_items_split": 0,
    }

    try:
        import fitz
        doc = fitz.open(pdf_path)
    except Exception as exc:
        log.warning("ai_analyze: cannot open PDF: %s", exc)
        return manifest

    try:
        page_count = len(doc)

        # ── Pass 1: Page-level layout ─────────────────────────────────────────
        for page_1 in range(1, min(page_count, MAX_PAGES) + 1):
            page_0 = page_1 - 1
            pnodes = _page_nodes(nodes, page_1)
            if len(pnodes) < 2:
                continue  # nothing to reorder on single-element pages

            img = _render_page(doc, page_0)
            if not img:
                continue

            try:
                corr = _analyze_page(client, img, pnodes)
            except Exception as exc:
                log.debug("Page analysis failed p=%d: %s", page_1, exc)
                continue

            if not corr:
                continue

            stats["pages_analyzed"] += 1

            # Reading order
            ro = corr.get("reading_order")
            if ro and isinstance(ro, list):
                orig = [n["id"] for n in pnodes]
                if ro != orig:
                    nodes = _apply_reading_order(nodes, page_1, ro)
                    stats["reading_order_corrections"] += 1

            # Layout tables
            for tid in (corr.get("layout_tables") or []):
                nodes = _patch(nodes, tid, {"tag": "Artifact", "decorative": True})
                stats["layout_tables_detected"] += 1

            # Caption → Figure pairing
            pairs = corr.get("caption_figure_pairs") or []
            if pairs:
                nodes = _apply_caption_figure_pairs(nodes, pairs)
                stats["captions_grouped"] += len(pairs)

            # Footnote pairs (record in manifest source for writeback)
            fn_pairs = corr.get("footnote_pairs") or []
            if fn_pairs:
                stats["footnote_pairs"] += len(fn_pairs)
                # Store on manifest source so writeback can wire Link annotations
                manifest.setdefault("source", {}).setdefault("footnotePairs", []).extend(fn_pairs)

        # ── Pass 2: Table header analysis ─────────────────────────────────────
        # Iterate over a snapshot — nodes list may have been mutated in Pass 1
        for node in list(_flat(nodes)):
            if node.get("tag") != "Table" or node.get("decorative"):
                continue
            page_0 = (node.get("page") or 1) - 1
            try:
                analysis = _analyze_table(client, doc, node, page_0)
            except Exception as exc:
                log.debug("Table analysis failed node=%s: %s", node.get("id"), exc)
                continue
            if not analysis:
                continue
            stats["tables_analyzed"] += 1
            cells_changed = len(analysis.get("cells") or [])
            nodes = _apply_table_analysis(nodes, node["id"], analysis)
            if analysis.get("is_layout_table"):
                stats["layout_tables_detected"] += 1
            else:
                stats["header_cells_updated"] += cells_changed

        # ── Pass 3: Formula / figure alt text ─────────────────────────────────
        for node in list(_flat(nodes)):
            tag = node.get("tag")
            if tag not in ("Formula", "Figure"):
                continue
            if node.get("alt") or node.get("decorative") or node.get("imageOfText"):
                continue  # already handled by describe.py
            page_0 = (node.get("page") or 1) - 1
            try:
                res = _analyze_formula(client, doc, node, page_0)
            except Exception as exc:
                log.debug("Formula analysis failed node=%s: %s", node.get("id"), exc)
                continue
            if not res:
                continue
            p: dict[str, Any] = {}
            if res.get("decorative"):
                p["decorative"] = True
            elif res.get("is_math"):
                alt   = (res.get("alt_text") or "").strip()
                latex = (res.get("latex")    or "").strip()
                if alt:
                    p["alt"]  = alt
                    p["tag"]  = "Formula"
                if latex:
                    p["latex"] = latex
            elif res.get("alt_text"):
                p["alt"] = res["alt_text"].strip()
            if p:
                nodes = _patch(nodes, node["id"], p)
                stats["formulas_described"] += 1

        # ── Pass 4: List item Lbl / LBody ─────────────────────────────────────
        for node in list(_flat(nodes)):
            if node.get("tag") != "LI" or node.get("children"):
                continue  # already has children — skip
            page_0 = (node.get("page") or 1) - 1
            try:
                res = _analyze_list_item(client, doc, node, page_0)
            except Exception as exc:
                log.debug("List item analysis failed node=%s: %s", node.get("id"), exc)
                continue
            label = (res.get("label") or "") if res else ""
            if label:
                nodes = _apply_lbl_lbody(nodes, node["id"], label)
                stats["list_items_split"] += 1

    finally:
        doc.close()

    log.info(
        "ai_analyze complete: pages=%d ro_fixes=%d layout_tables=%d "
        "captions=%d footnotes=%d tables=%d headers=%d formulas=%d lists=%d",
        stats["pages_analyzed"], stats["reading_order_corrections"],
        stats["layout_tables_detected"], stats["captions_grouped"],
        stats["footnote_pairs"], stats["tables_analyzed"],
        stats["header_cells_updated"], stats["formulas_described"],
        stats["list_items_split"],
    )

    return {**manifest, "nodes": nodes, "source": {**manifest.get("source", {}), "aiAnalysis": stats}}
