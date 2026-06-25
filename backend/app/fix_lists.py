"""Nested list structure repair (WCAG 1.3.1 / PDF/UA 7.7).

PDF/UA requires multi-level lists to use nested L elements:

  L → LI → LBody → L → LI → LBody → …

OpenDataLoader often outputs nested list items as flat siblings:

  L → LI (top level)
  L → LI (should be nested, but isn't)

This module detects nesting candidates by comparing the left edge of each
node's bounding box against the list container's left edge.  Items whose
bbox.left is indented by ≥ INDENT_THRESHOLD points beyond the list's
baseline are wrapped in a sub-L element attached to the previous LI/LBody.

It also detects paragraph nodes (P) immediately inside a list item that
themselves start with a bullet or numbered marker, indicating a sub-list
that the layout engine flattened.

No AI needed — bbox geometry is sufficient for reliable nesting detection.
"""

from __future__ import annotations

import re

INDENT_THRESHOLD = 12.0   # pt — minimum additional left indent to be "nested"

# Bullet / number patterns that indicate a sub-list item
_BULLET_RE = re.compile(
    r"^\s*(?:"
    r"[•‣▪▫●○–—\-\*]"  # bullet chars
    r"|\d{1,2}[\.\)]"          # 1. or 1)
    r"|[a-z][\.\)]"            # a. or a)
    r"|[ivxlc]+[\.\)]"         # roman numerals i. ii. iii.
    r")\s+",
    re.I | re.UNICODE,
)


def _bbox_left(node: dict) -> float | None:
    bbox = node.get("bbox")
    if bbox and len(bbox) >= 1:
        try:
            return float(bbox[0])
        except (TypeError, ValueError):
            pass
    return None


def _node_text(node: dict) -> str:
    return (node.get("text") or "").strip()


def _starts_with_bullet(text: str) -> bool:
    return bool(_BULLET_RE.match(text))


def _make_sub_list(items: list[dict], parent_id: str) -> dict:
    """Wrap items in a sub-L element."""
    first = items[0]
    return {
        "id": f"{parent_id}_sublist",
        "tag": "L",
        "page": first.get("page"),
        "bbox": first.get("bbox"),
        "source": {"type": "nested_list_auto"},
        "children": items,
    }


def _fix_list_node(list_node: dict) -> dict:
    """Repair one L node's children for proper nesting."""
    children = list(list_node.get("children") or [])
    if not children:
        return list_node

    # Determine the baseline left edge from the first LI/LBody
    base_left: float | None = None
    for child in children:
        left = _bbox_left(child)
        if left is not None:
            base_left = left
            break

    if base_left is None:
        return list_node  # no bbox info — can't determine nesting

    result: list[dict] = []
    pending_sub: list[dict] = []  # accumulated sub-list items
    last_li: dict | None = None   # most recent top-level LI to attach sub-L to

    def _flush_sub():
        nonlocal last_li
        if not pending_sub or last_li is None:
            result.extend(pending_sub)
            pending_sub.clear()
            return
        # Attach sub-list as a child of last_li (inside its LBody if present)
        sub_l = _make_sub_list(pending_sub, last_li["id"])
        # Try to append inside LBody child; otherwise directly to LI
        li_children = list(last_li.get("children") or [])
        lbody = next((c for c in li_children if c.get("tag") == "LBody"), None)
        if lbody:
            lbody_children = list(lbody.get("children") or []) + [sub_l]
            lbody = {**lbody, "children": lbody_children}
            li_children = [lbody if c.get("tag") == "LBody" else c for c in li_children]
        else:
            li_children = li_children + [sub_l]
        # Replace last_li in result
        updated_li = {**last_li, "children": li_children}
        result[-1] = updated_li
        last_li = updated_li
        pending_sub.clear()

    for child in children:
        tag = child.get("tag", "")
        left = _bbox_left(child)
        text = _node_text(child)

        is_indented = (
            left is not None
            and base_left is not None
            and (left - base_left) >= INDENT_THRESHOLD
        )

        is_sub_bullet = (
            tag in ("P", "LI")
            and _starts_with_bullet(text)
            and pending_sub  # only if already accumulating a sub-list
        )

        if is_indented or is_sub_bullet:
            # Convert P → LI if it's a paragraph that should be a list item
            if tag == "P":
                child = {**child, "tag": "LI"}
            pending_sub.append(child)
        else:
            if pending_sub:
                _flush_sub()
            # Recurse into this child if it's itself a list
            if tag == "L":
                child = _fix_list_node(child)
            elif tag == "LI":
                child = _fix_list_node_li(child)
            result.append(child)
            if tag in ("LI", "L"):
                last_li = child

    if pending_sub:
        _flush_sub()

    return {**list_node, "children": result}


def _fix_list_node_li(li_node: dict) -> dict:
    """Recursively fix nesting within an LI node."""
    children = list(li_node.get("children") or [])
    new_children = []
    for child in children:
        if child.get("tag") == "L":
            child = _fix_list_node(child)
        elif child.get("tag") == "LBody":
            lbody_children = list(child.get("children") or [])
            new_lbody_children = []
            for lbc in lbody_children:
                if lbc.get("tag") == "L":
                    lbc = _fix_list_node(lbc)
                new_lbody_children.append(lbc)
            child = {**child, "children": new_lbody_children}
        new_children.append(child)
    return {**li_node, "children": new_children}


def _walk(nodes: list[dict]) -> tuple[list[dict], int]:
    """Walk manifest nodes and fix all L nodes."""
    result = []
    fixed = 0
    for node in nodes:
        if node.get("tag") == "L":
            orig_children = node.get("children") or []
            repaired = _fix_list_node(node)
            if repaired.get("children") != orig_children:
                fixed += 1
            node = repaired
        elif node.get("children"):
            new_children, sub_fixed = _walk(node["children"])
            fixed += sub_fixed
            node = {**node, "children": new_children}
        result.append(node)
    return result, fixed


def fix_nested_lists(manifest: dict) -> dict:
    """Detect and repair nested list structure throughout the manifest.

    Returns the manifest unchanged if no nesting is found.
    """
    nodes = manifest.get("nodes", [])
    if not nodes:
        return manifest

    new_nodes, fixed = _walk(nodes)
    if not fixed:
        return manifest

    src = dict(manifest.get("source") or {})
    src["nestedListsFixed"] = fixed
    return {**manifest, "nodes": new_nodes, "source": src}
