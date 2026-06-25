"""Table header scope assignment (Sprint 15 — PDF/UA §7.5).

PDF/UA-1 clause 7.5 / WCAG 1.3.1 require that table header cells (TH)
carry a /Scope attribute so screen readers can associate data cells with
their headers:

  /Scope /Column  — header applies to a column of data cells below it
  /Scope /Row     — header applies to a row of data cells to its right
  /Scope /Both    — top-left corner cell (applies to both axes)

This module enriches manifest Table nodes with scope information so that
writeback.py can write the /Scope attribute when building TH struct elements.

Heuristic:
  • TH cells in the first row → Column
  • TH cells in the first column (non-first row) → Row
  • TH in position [0][0] with both row AND column headers → Both
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _has_col_headers(rows: list[dict]) -> bool:
    """True if first row contains TH cells."""
    if not rows:
        return False
    first_row = rows[0].get("children") or []
    return any(c.get("tag") == "TH" for c in first_row)


def _has_row_headers(rows: list[dict]) -> bool:
    """True if non-first rows have a TH in the first cell."""
    for row in rows[1:]:
        cells = row.get("children") or []
        if cells and cells[0].get("tag") == "TH":
            return True
    return False


def _assign_scope_to_rows(rows: list[dict], col_headers: bool, row_headers: bool) -> list[dict]:
    new_rows = []
    for ri, row in enumerate(rows):
        cells = row.get("children") or []
        new_cells = []
        for ci, cell in enumerate(cells):
            if cell.get("tag") != "TH":
                new_cells.append(cell)
                continue
            # Determine scope
            if ri == 0 and ci == 0 and col_headers and row_headers:
                scope = "Both"
            elif ri == 0 and col_headers:
                scope = "Column"
            elif ci == 0 and row_headers:
                scope = "Row"
            else:
                scope = "Column"  # default for any other TH
            new_cells.append({**cell, "scope": scope})
        new_rows.append({**row, "children": new_cells})
    return new_rows


def _process_table(node: dict) -> dict:
    """Add scope attributes to all TH cells in a Table node."""
    children = node.get("children") or []

    # Collect TR nodes (may be wrapped in THead/TBody/TFoot)
    all_rows: list[dict] = []
    non_row_children: list[dict] = []
    for child in children:
        if child.get("tag") == "TR":
            all_rows.append(child)
        elif child.get("tag") in ("THead", "TBody", "TFoot"):
            inner_rows = [c for c in (child.get("children") or []) if c.get("tag") == "TR"]
            all_rows.extend(inner_rows)
            non_row_children.append(child)
        else:
            non_row_children.append(child)

    if not all_rows:
        return node

    col_h = _has_col_headers(all_rows)
    row_h = _has_row_headers(all_rows)
    new_rows = _assign_scope_to_rows(all_rows, col_h, row_h)

    # Rebuild children with updated rows (keep THead/TBody/TFoot wrappers if present)
    if non_row_children and any(c.get("tag") in ("THead", "TBody", "TFoot") for c in non_row_children):
        # Patch back into wrappers
        row_idx = 0
        new_children = []
        for child in children:
            if child.get("tag") == "TR":
                new_children.append(new_rows[row_idx])
                row_idx += 1
            elif child.get("tag") in ("THead", "TBody", "TFoot"):
                inner = [c for c in (child.get("children") or []) if c.get("tag") == "TR"]
                patched_inner = new_rows[row_idx: row_idx + len(inner)]
                row_idx += len(inner)
                new_children.append({**child, "children": patched_inner})
            else:
                new_children.append(child)
        return {**node, "children": new_children}
    else:
        return {**node, "children": new_rows}


def _walk(nodes: list[dict]) -> list[dict]:
    result = []
    for n in nodes:
        if n.get("tag") == "Table":
            n = _process_table(n)
        if n.get("children"):
            n = {**n, "children": _walk(n["children"])}
        result.append(n)
    return result


def assign_table_scope(manifest: dict) -> dict:
    """Walk manifest and add /scope attribute to all TH cells.

    Called from autotag.py after fix_tables().
    """
    nodes = manifest.get("nodes", [])
    new_nodes = _walk(nodes)
    log.debug("table_scope: scope assignment pass complete")
    return {**manifest, "nodes": new_nodes}
