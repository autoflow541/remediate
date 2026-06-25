"""
Auto-detect table header rows in the manifest.

Rule: for each Table node, if its first TR has no TH children already,
mark every TD in that row as TH with scope=Col. This satisfies
WCAG 1.3.1 and PDF-UA for straightforward tabular data.

Called automatically during autotag — no user action required.
"""

from __future__ import annotations


def _looks_like_data_row(tr_children: list) -> bool:
    """Return True if a table row looks like a data row, not a header row.

    Heuristic: if ANY cell's text starts with a digit (e.g. "1.1.1", "2024",
    "3.") it is almost certainly a data row — real header cells contain
    column labels like "Criterion", "Level", "Status", not numeric IDs.
    """
    for cell in tr_children:
        text = (cell.get("text") or "").lstrip()
        if text and text[0].isdigit():
            return True
    return False


def _fix_table(table: dict) -> dict:
    """Promote first-row TDs to TH if no headers exist yet."""
    children = table.get("children") or []

    # Find index of first TR child
    first_tr_idx = next(
        (i for i, c in enumerate(children) if c.get("tag") == "TR"), None
    )
    if first_tr_idx is None:
        return table

    first_tr = children[first_tr_idx]
    tr_children = first_tr.get("children") or []

    # Already has at least one TH — respect existing markup
    if any(c.get("tag") == "TH" for c in tr_children):
        return table

    # Skip promotion if the first row looks like a data row.
    # This prevents falsely tagging the first data row as a header when the
    # actual header row was missed by the layout detector (e.g. white text on
    # a dark-coloured background that the layout model skipped).
    if _looks_like_data_row(tr_children):
        return table

    # Promote all TDs in the first row to TH with column scope
    new_tr_children = [
        {**c, "tag": "TH", "scope": "Col"} if c.get("tag") == "TD" else c
        for c in tr_children
    ]
    new_first_tr = {**first_tr, "children": new_tr_children}
    new_children = (
        children[:first_tr_idx]
        + [new_first_tr]
        + children[first_tr_idx + 1 :]
    )
    return {**table, "children": new_children}


def _walk(nodes: list) -> list:
    result = []
    for node in nodes:
        if node.get("tag") == "Table":
            node = _fix_table(node)
        children = node.get("children")
        if children:
            node = {**node, "children": _walk(children)}
        result.append(node)
    return result


def auto_tag_tables(manifest: dict) -> dict:
    """Return a new manifest with table header rows promoted."""
    return {**manifest, "nodes": _walk(manifest.get("nodes") or [])}
