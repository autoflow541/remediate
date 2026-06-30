"""Table structure checker — WCAG 1.3.1 / PDF/UA-1 §7.5.

Audits tables in the PDF structure tree for common accessibility failures:

  1. Missing header cells (no TH in table — data table with no headers)
  2. TH cells missing /Scope attribute (§7.5 / WCAG H63)
  3. Complex tables (more than one row of headers or header column + row)
     that lack /Headers attributes on TD cells
  4. Tables with no caption or /Summary

Each finding includes the table's page, approximate position, and remediation
guidance.
"""

from __future__ import annotations

from typing import Any


def _obj_tag(obj) -> str:
    try:
        return str(obj.get("/S", "")).lstrip("/")
    except Exception:
        return ""


def _page_of(obj, page_map: dict) -> int | None:
    try:
        pg = obj.get("/Pg")
        if pg is not None:
            return page_map.get(id(pg))
    except Exception:
        pass
    return None


def _get_kids(obj) -> list:
    """Return child struct elements of obj."""
    import pikepdf
    try:
        kids = obj.get("/K")
    except Exception:
        return []
    if kids is None:
        return []
    if isinstance(kids, pikepdf.Array):
        result = []
        for k in kids:
            try:
                if isinstance(k, (pikepdf.Dictionary, pikepdf.Object)):
                    result.append(k)
            except Exception:
                pass
        return result
    if isinstance(kids, pikepdf.Dictionary):
        return [kids]
    return []


def _walk_tables(obj, page_map: dict, tables: list, _depth: int = 0) -> None:
    """Walk the struct tree collecting Table elements."""
    if _depth > 80:
        return
    tag = _obj_tag(obj)
    if tag == "Table":
        tables.append((obj, _page_of(obj, page_map)))
        return  # don't recurse into table — analysed separately
    for kid in _get_kids(obj):
        _walk_tables(kid, page_map, tables, _depth + 1)


def _analyse_table(table_obj, page: int | None) -> list[dict]:
    """Return issues found in a single Table struct element."""
    issues: list[dict] = []

    # Flatten rows from TR children (may be inside THead/TBody/TFoot)
    rows: list = []

    def _collect_rows(obj, depth=0):
        if depth > 10:
            return
        tag = _obj_tag(obj)
        if tag == "TR":
            rows.append(obj)
            return
        for kid in _get_kids(obj):
            _collect_rows(kid, depth + 1)

    _collect_rows(table_obj)

    if not rows:
        return []  # empty or non-standard table structure

    # Collect cells per row
    row_cells: list[list[dict]] = []
    has_any_th = False
    has_caption = False

    for row in rows:
        cells = []
        for kid in _get_kids(row):
            tag = _obj_tag(kid)
            if tag in ("TH", "TD"):
                scope = None
                headers = None
                try:
                    attr_obj = kid.get("/A")
                    if attr_obj is not None:
                        import pikepdf
                        if isinstance(attr_obj, pikepdf.Array):
                            for a in attr_obj:
                                try:
                                    s = a.get("/Scope")
                                    if s:
                                        scope = str(s).lstrip("/")
                                    h = a.get("/Headers")
                                    if h:
                                        headers = h
                                except Exception:
                                    pass
                        else:
                            try:
                                s = attr_obj.get("/Scope")
                                if s:
                                    scope = str(s).lstrip("/")
                                h = attr_obj.get("/Headers")
                                if h:
                                    headers = h
                            except Exception:
                                pass
                except Exception:
                    pass
                cells.append({"tag": tag, "scope": scope, "headers": headers})
                if tag == "TH":
                    has_any_th = True
        row_cells.append(cells)

    # Check for Caption sibling
    for kid in _get_kids(table_obj):
        if _obj_tag(kid) == "Caption":
            has_caption = True
            break

    # ── Issue 1: No TH cells at all (data table with no header row) ──────────
    if not has_any_th:
        # Only flag if table has >= 2 rows and >= 2 columns (skip trivial tables)
        nrows = len(row_cells)
        ncols = max((len(r) for r in row_cells), default=0)
        if nrows >= 2 and ncols >= 2:
            issues.append({
                "type": "missing_headers",
                "page": page,
                "description": (
                    "Table has no header cells (TH). All data tables must have at "
                    "least one row or column of TH cells. (WCAG 1.3.1 / PDF/UA §7.5)"
                ),
                "suggestion": "Mark the first row or column cells as TH and assign Scope=Row or Scope=Column.",
            })

    # ── Issue 2: TH cells missing /Scope ─────────────────────────────────────
    th_without_scope: int = 0
    for row in row_cells:
        for cell in row:
            if cell["tag"] == "TH" and not cell["scope"]:
                th_without_scope += 1

    if th_without_scope:
        issues.append({
            "type": "th_missing_scope",
            "page": page,
            "count": th_without_scope,
            "description": (
                f"{th_without_scope} TH cell(s) are missing a /Scope attribute. "
                "Each TH must declare Scope=Row, Scope=Column, Scope=Both, or "
                "Scope=None. (PDF/UA §7.5 / WCAG H63)"
            ),
            "suggestion": "Add Scope=Column to header cells in the first row; Scope=Row to cells in the first column.",
        })

    # ── Issue 3: Complex table — TDs missing /Headers ────────────────────────
    # Heuristic: complex = headers in both first row AND first column
    header_row = bool(row_cells and any(c["tag"] == "TH" for c in row_cells[0]))
    header_col = bool(
        len(row_cells) > 1 and row_cells[1] and row_cells[1][0]["tag"] == "TH"
    )
    is_complex = header_row and header_col

    if is_complex:
        td_without_headers = sum(
            1
            for row in row_cells
            for cell in row
            if cell["tag"] == "TD" and not cell["headers"]
        )
        if td_without_headers:
            issues.append({
                "type": "complex_table_missing_headers_attr",
                "page": page,
                "count": td_without_headers,
                "description": (
                    f"Complex table (headers in both row and column) has {td_without_headers} "
                    "TD cell(s) without /Headers attributes. In complex tables, each data cell "
                    "must reference its header cells by ID. (WCAG 1.3.1 / PDF/UA §7.5)"
                ),
                "suggestion": (
                    "Assign unique IDs to TH cells and add Headers arrays to TD cells "
                    "listing the IDs of all applicable header cells."
                ),
            })

    # ── Issue 4: No caption ───────────────────────────────────────────────────
    nrows = len(row_cells)
    ncols = max((len(r) for r in row_cells), default=0)
    if not has_caption and nrows >= 2 and ncols >= 2:
        issues.append({
            "type": "missing_caption",
            "page": page,
            "description": (
                "Table has no Caption element. A brief caption or summary helps "
                "screen reader users understand the table's purpose before entering it. "
                "(Best practice / WCAG 1.3.1)"
            ),
            "suggestion": "Add a Caption struct element as the first child of the Table, or provide a /Summary entry.",
        })

    return issues


def check_table_structure(pdf_path: str) -> list[dict[str, Any]]:
    """Return a list of table accessibility issues found in *pdf_path*.

    Each issue dict contains: type, page, description, suggestion, and
    optionally count.
    """
    try:
        import pikepdf
    except ImportError:
        return []

    try:
        pdf = pikepdf.open(pdf_path)
    except Exception:
        return []

    page_map: dict[int, int] = {}
    try:
        for i, page in enumerate(pdf.pages):
            page_map[id(page.obj)] = i + 1
    except Exception:
        pass

    tables: list = []
    try:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is not None:
            _walk_tables(struct_root, page_map, tables)
    except Exception:
        pass
    finally:
        pdf.close()

    issues: list[dict] = []
    for table_obj, page in tables:
        issues.extend(_analyse_table(table_obj, page))

    return issues
