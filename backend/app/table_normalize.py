"""table_normalize.py — make tagged tables rectangular (PDF/UA 7.2-10/42/43).

Real-world tagged tables routinely have rows that don't span the same number of
columns (a header row missing a cell, a merged cell tagged without its colspan,
a stray non-cell element inside a row). veraPDF fails these:

  7.2-10  TR may contain only TH and TD elements
  7.2-42  Regular table rows shall have the same number of columns
  7.2-43  (span-aware variant of 7.2-42)

Two mechanical, content-preserving repairs per table:

  * any non-TH/TD child of a TR is wrapped in a new TD (7.2-10);
  * a span-aware occupancy grid computes the table's true width, then every row
    that falls short is padded with empty TD (or TH, if the row is all-header)
    structure elements (7.2-42/43). Empty cells are valid and announced as
    blank by assistive tech — far better than a non-conformant grid.

Rowspans carried down from earlier rows are counted, so padding is correct for
merged tables too. No content, MCIDs, or existing cells are altered.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_ROW_CONTAINERS = {"THead", "TBody", "TFoot"}


def _s(o) -> str:
    try:
        v = o.get("/S")
        return str(v).lstrip("/") if v is not None else ""
    except Exception:
        return ""


def _kids(o, pikepdf):
    try:
        k = o.get("/K")
    except Exception:
        return []
    if k is None:
        return []
    return list(k) if isinstance(k, pikepdf.Array) else [k]


def _span(cell, key: str, pikepdf) -> int:
    """Read /ColSpan or /RowSpan from a cell's /A Table attribute object(s)."""
    try:
        a = cell.get("/A")
    except Exception:
        return 1
    if a is None:
        return 1
    objs = list(a) if isinstance(a, pikepdf.Array) else [a]
    for o in objs:
        if not hasattr(o, "get"):
            continue
        try:
            v = o.get(key)
            if v is not None:
                return max(1, int(v))
        except Exception:
            continue
    return 1


def _rows_of(table, pikepdf) -> list:
    """Ordered TR elements of a table, descending through THead/TBody/TFoot."""
    rows = []
    for c in _kids(table, pikepdf):
        if not hasattr(c, "get"):
            continue
        tag = _s(c)
        if tag == "TR":
            rows.append(c)
        elif tag in _ROW_CONTAINERS:
            for cc in _kids(c, pikepdf):
                if hasattr(cc, "get") and _s(cc) == "TR":
                    rows.append(cc)
    return rows


def normalize_tables(pdf_path: str) -> tuple[int, list[str]]:
    """Make every tagged table rectangular. Returns (changes, notes)."""
    try:
        import pikepdf
        from pikepdf import Array, Dictionary, Name
    except ImportError:
        return 0, []

    padded_cells = 0
    wrapped = 0
    tables_fixed = 0

    try:
        with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
            root = pdf.Root.get("/StructTreeRoot")
            if root is None:
                return 0, []

            tables = []
            seen: set = set()
            stack = [root]
            while stack:
                o = stack.pop()
                if not hasattr(o, "get"):
                    continue
                try:
                    og = o.objgen
                    if og != (0, 0):
                        if og in seen:
                            continue
                        seen.add(og)
                except Exception:
                    pass
                if _s(o) == "Table":
                    tables.append(o)
                stack.extend(_kids(o, pikepdf))

            for table in tables:
                rows = _rows_of(table, pikepdf)
                if not rows:
                    continue
                changed = False

                # ── 7.2-10: wrap non-cell children of each TR in a TD ─────────
                for tr in rows:
                    new_k = []
                    for c in _kids(tr, pikepdf):
                        if hasattr(c, "get") and _s(c) in ("TD", "TH"):
                            new_k.append(c)
                        elif hasattr(c, "get"):
                            td = pdf.make_indirect(Dictionary(
                                Type=Name.StructElem, S=Name.TD, P=tr, K=Array([c])))
                            try:
                                c[Name("/P")] = td
                            except Exception:
                                pass
                            new_k.append(td)
                            wrapped += 1
                            changed = True
                        else:
                            new_k.append(c)  # ints/MCIDs — leave as-is
                    if changed:
                        tr[Name("/K")] = Array(new_k)

                # ── span-aware occupancy grid ─────────────────────────────────
                # carry[col] = rows this column stays occupied by a rowspan,
                # counting from (and including) the current row. Each row's
                # occupied-column count = its own cells' colspans + columns still
                # covered by rowspans from above — the same count veraPDF checks.
                carry: dict[int, int] = {}
                row_cells: list[list] = []
                row_covered: list[int] = []
                width = 0
                for tr in rows:
                    cells = [c for c in _kids(tr, pikepdf)
                             if hasattr(c, "get") and _s(c) in ("TD", "TH")]
                    covered: set[int] = {c for c, v in carry.items() if v > 0}
                    col = 0
                    for cell in cells:
                        while col in covered:
                            col += 1
                        cs = _span(cell, "/ColSpan", pikepdf)
                        rs = _span(cell, "/RowSpan", pikepdf)
                        for cc in range(col, col + cs):
                            covered.add(cc)
                            if rs > 1:
                                carry[cc] = rs        # rs rows incl. this one
                        col += cs
                    row_cells.append(cells)
                    row_covered.append(len(covered))
                    if covered:
                        width = max(width, max(covered) + 1)
                    # this row consumes one row of every active rowspan
                    carry = {k: v - 1 for k, v in carry.items() if v - 1 > 0}

                # ── pad short rows to `width` with empty cells ────────────────
                for tr, cells, covered_n in zip(rows, row_cells, row_covered):
                    if covered_n >= width:
                        continue
                    all_header = bool(cells) and all(_s(c) == "TH" for c in cells)
                    tag = Name.TH if all_header else Name.TD
                    additions = []
                    for _ in range(width - covered_n):
                        cell = pdf.make_indirect(Dictionary(
                            Type=Name.StructElem, S=tag, P=tr))
                        additions.append(cell)
                        padded_cells += 1
                    k = tr.get("/K")
                    if isinstance(k, pikepdf.Array):
                        for a in additions:
                            k.append(a)
                    elif k is not None:
                        tr[Name("/K")] = Array([k, *additions])
                    else:
                        tr[Name("/K")] = Array(additions)
                    changed = True

                if changed:
                    tables_fixed += 1

            if padded_cells or wrapped:
                pdf.save()
    except Exception as exc:
        log.warning("table_normalize: %s", exc)
        return 0, []

    notes = []
    if padded_cells or wrapped:
        parts = []
        if padded_cells:
            parts.append(f"padded {padded_cells} cell(s) to square the grid")
        if wrapped:
            parts.append(f"wrapped {wrapped} stray row element(s) in cells")
        notes.append(f"Normalized {tables_fixed} table(s): " + ", ".join(parts)
                     + " (PDF/UA 7.2-10/42/43)")
    return padded_cells + wrapped, notes
