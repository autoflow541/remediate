"""Table analysis for accessible tagging.  [Phase 4]

OpenDataLoader returns every table cell as a data cell (TD) — it never guesses
which cells are headers, because that is genuine human judgment. This module
proposes sensible header cells (and the /Scope they need) so the studio arrives
pre-filled, and computes the Headers/ID associations that *complex* tables need
when /Scope alone is insufficient (spanning cells, or both row and column
headers).

The studio can override any proposal; `header` is a suggestion the human
confirms. We keep the analysis here (not in autotag's raw layout pass) so the
two concerns stay separate and testable.

Mutates the manifest in place and returns a summary report.
"""

from __future__ import annotations

from typing import Any


def _cells_of(table: dict) -> list[dict]:
    """All cell nodes of a table, flattened across its TR rows."""
    cells = []
    for row in table.get("children", []) or []:
        if row.get("tag") == "TR":
            for cell in row.get("children", []) or []:
                cells.append(cell)
    return cells


def _grid_dims(table: dict, cells: list[dict]) -> tuple[int, int]:
    rows = table.get("rows")
    cols = table.get("cols")
    if not rows:
        rows = max((int(c.get("row", 1)) + int(c.get("rowSpan", 1)) - 1 for c in cells), default=0)
    if not cols:
        cols = max((int(c.get("col", 1)) + int(c.get("colSpan", 1)) - 1 for c in cells), default=0)
    return int(rows), int(cols)


def _build_grid(cells: list[dict], n_rows: int, n_cols: int) -> dict[tuple[int, int], dict]:
    """Map every (row, col) the table occupies to the cell covering it, honoring
    row/column spans (1-indexed)."""
    grid: dict[tuple[int, int], dict] = {}
    for cell in cells:
        r0 = int(cell.get("row", 1))
        c0 = int(cell.get("col", 1))
        rs = max(1, int(cell.get("rowSpan", 1)))
        cs = max(1, int(cell.get("colSpan", 1)))
        for r in range(r0, r0 + rs):
            for c in range(c0, c0 + cs):
                grid.setdefault((r, c), cell)
    return grid


def _set_header(cell: dict, scope: str) -> None:
    cell["tag"] = "TH"
    cell["header"] = True
    # If a cell is both a row and column header (the table corner), widen scope.
    existing = cell.get("scope")
    if existing and existing != scope:
        cell["scope"] = "Both"
    else:
        cell["scope"] = scope


def _analyze_table(
    table: dict,
    table_index: int,
    detect_row_headers: bool,
    summary: dict,
) -> None:
    cells = _cells_of(table)
    if not cells:
        return
    n_rows, n_cols = _grid_dims(table, cells)
    grid = _build_grid(cells, n_rows, n_cols)
    summary["tables"] += 1

    # --- regularity check (every grid position covered exactly once) ---
    missing = [(r, c) for r in range(1, n_rows + 1) for c in range(1, n_cols + 1)
               if (r, c) not in grid]
    if missing:
        summary["irregular"] += 1
        table.setdefault("warnings", []).append(
            f"table has {len(missing)} uncovered cell position(s); header "
            "associations may be incomplete"
        )

    has_spans = any(int(c.get("rowSpan", 1)) > 1 or int(c.get("colSpan", 1)) > 1
                    for c in cells)

    # --- propose header cells ---
    header_cols = set()  # column indices that are header columns
    header_rows = {1}    # first row proposed as column headers
    for c in range(1, n_cols + 1):
        cell = grid.get((1, c))
        if cell is not None:
            _set_header(cell, "Column")
            summary["headers_proposed"] += 1

    if detect_row_headers and n_cols > 1:
        header_cols.add(1)
        for r in range(1, n_rows + 1):
            cell = grid.get((r, 1))
            if cell is not None:
                _set_header(cell, "Row")
                summary["headers_proposed"] += 1

    # --- complexity: spans, or both row+column headers -> use Headers/IDs ---
    complex_table = has_spans or bool(header_cols)
    if not complex_table:
        return
    summary["complex"] += 1

    # Assign a stable ID to every header cell.
    for cell in cells:
        if cell.get("header"):
            r0, c0 = int(cell.get("row", 1)), int(cell.get("col", 1))
            cell["headerId"] = f"t{table_index}r{r0}c{c0}"

    # For each data cell, associate the column header(s) above and row header(s)
    # to its left, resolved through the span-aware grid.
    for cell in cells:
        if cell.get("header"):
            continue
        r0, c0 = int(cell.get("row", 1)), int(cell.get("col", 1))
        cs = max(1, int(cell.get("colSpan", 1)))
        rs = max(1, int(cell.get("rowSpan", 1)))
        ids: list[str] = []
        for c in range(c0, c0 + cs):
            for hr in sorted(header_rows):
                h = grid.get((hr, c))
                if h is not None and h.get("headerId") and h["headerId"] not in ids:
                    ids.append(h["headerId"])
        for r in range(r0, r0 + rs):
            for hc in sorted(header_cols):
                h = grid.get((r, hc))
                if h is not None and h.get("headerId") and h["headerId"] not in ids:
                    ids.append(h["headerId"])
        if ids:
            cell["headers"] = ids


def analyze_tables(manifest: dict, detect_row_headers: bool = False) -> dict:
    """Walk all tables in ``manifest`` and propose headers / associations.

    ``detect_row_headers`` also proposes the first column as row headers (off by
    default — first-row column headers are the reliable common case).

    Returns a summary report.
    """
    summary = {"tables": 0, "headers_proposed": 0, "complex": 0, "irregular": 0}

    def walk(nodes: list[dict]) -> None:
        idx = 0
        for node in nodes:
            if node.get("tag") == "Table":
                _analyze_table(node, idx, detect_row_headers, summary)
                idx += 1
            children = node.get("children")
            if children:
                walk(children)

    walk(manifest.get("nodes", []) or [])
    return summary
