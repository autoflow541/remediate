"""table_normalize.py — squaring irregular tagged tables (PDF/UA 7.2/7.5).

The span-aware grid math is the subtle part; these lock down the row-width and
padding behaviour that took several iterations to get right, plus the TH-scope
and non-cell-wrapping repairs.
"""

from __future__ import annotations

import pytest

pikepdf = pytest.importorskip("pikepdf")

from app.table_normalize import normalize_tables, _s, _kids, _rows_of


def _cell_counts(path):
    """Return [n_cells_per_row] for the first table in the PDF."""
    pdf = pikepdf.open(path)
    tables = []
    stack = [pdf.Root.StructTreeRoot]
    while stack:
        o = stack.pop()
        if hasattr(o, "get"):
            if _s(o) == "Table":
                tables.append(o)
            stack.extend(_kids(o, pikepdf))
    counts = []
    for tr in _rows_of(tables[0], pikepdf):
        counts.append(sum(1 for c in _kids(tr, pikepdf)
                          if hasattr(c, "get") and _s(c) in ("TD", "TH")))
    pdf.close()
    return counts


def test_short_rows_padded_to_table_width(make_table_pdf):
    # row 0 covers 3 cols, rows 1/2 cover 4 -> normalize pads row 0 to 4.
    path = make_table_pdf([
        ["TH", "TH", "TH"],
        ["TH", "TH", "TD", "TD"],
        ["TH", "TD", "TD"],
    ])
    n, notes = normalize_tables(path)
    assert n > 0
    assert _cell_counts(path) == [4, 4, 4]


def test_regular_table_not_padded(make_table_pdf):
    # A rectangular table gets no padding cells (it may still gain TH /Scope,
    # which is a separate, correct change — so we assert on cell counts).
    path = make_table_pdf([["TH", "TH"], ["TD", "TD"], ["TD", "TD"]])
    normalize_tables(path)
    assert _cell_counts(path) == [2, 2, 2]


def test_colspan_counts_toward_width(make_table_pdf):
    # row 0: one TH spanning 3 cols == width 3; row 1: three single cells == 3.
    # Span-aware, so no padding is added despite differing raw cell counts.
    path = make_table_pdf([
        [("TH", 3, 1)],
        ["TD", "TD", "TD"],
    ])
    normalize_tables(path)
    assert _cell_counts(path) == [1, 3]  # unchanged: no padding cells added


def test_th_gets_scope(make_table_pdf):
    path = make_table_pdf([["TH", "TH"], ["TH", "TD"]])
    normalize_tables(path)
    pdf = pikepdf.open(path)
    scopes = []
    stack = [pdf.Root.StructTreeRoot]
    while stack:
        o = stack.pop()
        if hasattr(o, "get"):
            if _s(o) == "TH":
                a = o.get("/A")
                objs = list(a) if isinstance(a, pikepdf.Array) else ([a] if a else [])
                scopes.append(any(hasattr(x, "get") and x.get("/Scope") is not None
                                  for x in objs))
            stack.extend(_kids(o, pikepdf))
    pdf.close()
    assert scopes and all(scopes), "every TH must carry a /Scope"
