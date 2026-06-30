"""PDF one-click patch actions (P1 feature — competitor parity).

Provides targeted, post-remediation fixes that can be applied to an already-
remediated PDF without a full re-tag/re-write cycle:

  patch_metadata(pdf_path, title, lang, author, subject)
      Set /Title, /Lang, /Author, /Subject and the matching XMP metadata.
      Writes DisplayDocTitle = true so AT announces the title on open.

  patch_heading_levels(pdf_path)
      Auto-repair skipped heading levels (H1 → H3 → H2 → H3) so the
      hierarchy is contiguous.  Does NOT change heading content — only
      adjusts the /S name of each H struct element in the structure tree.

Both functions accept an input path and write the result back IN PLACE
(atomic replace via a temp file), returning a brief summary dict.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Metadata patch
# ---------------------------------------------------------------------------

def patch_metadata(
    pdf_path: str,
    title: str = "",
    lang: str = "",
    author: str = "",
    subject: str = "",
) -> dict[str, Any]:
    """Set document metadata on pdf_path (in-place).

    At minimum, title and lang should be supplied — they are the two fields
    checked by both veraPDF and the Studio's metadata checker as errors.

    Returns a summary: {patched_fields: [...], ok: bool}.
    """
    try:
        import pikepdf
    except ImportError:
        return {"ok": False, "error": "pikepdf not installed"}

    patched: list[str] = []
    try:
        pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    try:
        # --- DocInfo dictionary ---
        if title:
            pdf.docinfo["/Title"] = title
            patched.append("Title")
        if author:
            pdf.docinfo["/Author"] = author
            patched.append("Author")
        if subject:
            pdf.docinfo["/Subject"] = subject
            patched.append("Subject")

        # --- /Lang on the document catalog (Root) ---
        if lang:
            pdf.Root["/Lang"] = pikepdf.String(lang)
            patched.append("Lang")

        # --- ViewerPreferences: DisplayDocTitle = true ---
        try:
            if "/ViewerPreferences" not in pdf.Root:
                pdf.Root["/ViewerPreferences"] = pdf.make_indirect(pikepdf.Dictionary())
            vp = pdf.Root["/ViewerPreferences"]
            vp["/DisplayDocTitle"] = pikepdf.Boolean(True)
            if "ViewerPreferences.DisplayDocTitle" not in patched:
                patched.append("ViewerPreferences.DisplayDocTitle")
        except Exception as _e:
            log.debug("Could not set DisplayDocTitle: %s", _e)

        # --- XMP metadata ---
        try:
            with pdf.open_metadata() as meta:
                if title:
                    meta["dc:title"] = title
                if author:
                    meta["dc:creator"] = [author]
                if subject:
                    meta["dc:description"] = subject
                if lang:
                    meta["dc:language"] = [lang]
        except Exception as _e:
            log.debug("XMP update skipped: %s", _e)

        # Atomic save via temp file to avoid corrupting on partial write.
        fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(pdf_path))
        os.close(fd)
        try:
            pdf.save(tmp)
            os.replace(tmp, pdf_path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    finally:
        pdf.close()

    log.info("patch_metadata: patched %r on %s", patched, pdf_path)
    return {"ok": True, "patched_fields": patched}


# ---------------------------------------------------------------------------
# Heading level repair
# ---------------------------------------------------------------------------

def patch_heading_levels(pdf_path: str) -> dict[str, Any]:
    """Repair skipped heading levels in the structure tree (in-place).

    Algorithm:
      1. Walk the entire structure tree and collect all H-struct elements
         in document order (DFS, pre-order).
      2. Compute the "corrected" level for each heading so that:
           - The first heading becomes H1 (or stays H1 if it already is).
           - No level may increase by more than 1 relative to the previous.
           - Going deeper than the previous is allowed only +1 step at a time.
           - Going back up is unrestricted (closing a section is fine).
      3. Rewrite /S on any element whose corrected level differs from the
         original.

    This is a conservative fix — it only closes gaps, never renumbers
    semantically different headings.  The document structure intent is
    preserved; only the gap-fillers are collapsed.

    Returns {ok, repairs_made, changes: [{from, to, text_preview}]}.
    """
    try:
        import pikepdf
    except ImportError:
        return {"ok": False, "error": "pikepdf not installed"}

    try:
        pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    heading_tags = {"/H", "/H1", "/H2", "/H3", "/H4", "/H5", "/H6"}

    # Collect (struct_element_object, original_level) in DFS order.
    collected: list[tuple[Any, int]] = []

    def _level(tag: str) -> int:
        if tag == "/H":
            return 1
        try:
            return int(tag[2])
        except (IndexError, ValueError):
            return 1

    def _text_preview(obj: Any) -> str:
        """Best-effort: grab /Alt or first K text run."""
        try:
            alt = obj.get("/Alt")
            if alt:
                return str(alt)[:60]
        except Exception:
            pass
        return ""

    def _walk(obj: Any, depth: int = 0) -> None:
        if depth > 100:
            return
        try:
            tag = str(obj.get("/S", ""))
        except Exception:
            return

        if tag in heading_tags:
            collected.append((obj, _level(tag)))

        # Recurse into /K (children)
        try:
            k = obj.get("/K")
            if k is None:
                return
            if isinstance(k, pikepdf.Array):
                for child in k:
                    try:
                        if isinstance(child, pikepdf.Dictionary):
                            _walk(child, depth + 1)
                        elif hasattr(child, "obj"):
                            _walk(child.obj, depth + 1)
                    except Exception:
                        pass
            elif isinstance(k, pikepdf.Dictionary):
                _walk(k, depth + 1)
            elif hasattr(k, "obj"):
                _walk(k.obj, depth + 1)
        except Exception:
            pass

    try:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            pdf.close()
            return {"ok": True, "repairs_made": 0, "changes": [], "note": "No structure tree"}
        _walk(struct_root)
    except Exception as exc:
        pdf.close()
        return {"ok": False, "error": f"Structure tree walk failed: {exc}"}

    if not collected:
        pdf.close()
        return {"ok": True, "repairs_made": 0, "changes": []}

    # Compute corrected levels.
    corrected: list[int] = []
    prev = 0
    for _, orig in collected:
        if prev == 0:
            # First heading — normalize to H1.
            corrected.append(1)
            prev = 1
        else:
            # Allow going deeper only by 1.
            if orig > prev + 1:
                new_level = prev + 1
            else:
                new_level = orig
            corrected.append(new_level)
            prev = new_level

    # Apply corrections.
    changes: list[dict] = []
    for (obj, orig), new_level in zip(collected, corrected):
        if new_level != orig:
            old_tag = f"/H{orig}" if orig != 1 else "/H1"
            new_tag = f"/H{new_level}"
            try:
                obj["/S"] = pikepdf.Name(new_tag)
                changes.append({
                    "from": f"H{orig}",
                    "to": f"H{new_level}",
                    "text_preview": _text_preview(obj),
                })
            except Exception as _e:
                log.debug("Could not rewrite heading tag: %s", _e)

    repairs = len(changes)
    if repairs > 0:
        fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(pdf_path))
        os.close(fd)
        try:
            pdf.save(tmp)
            os.replace(tmp, pdf_path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            pdf.close()
            raise
    pdf.close()

    log.info("patch_heading_levels: %d repair(s) on %s", repairs, pdf_path)
    return {"ok": True, "repairs_made": repairs, "changes": changes}


# ---------------------------------------------------------------------------
# Table header scope repair
# ---------------------------------------------------------------------------

def patch_table_headers(pdf_path: str) -> dict[str, Any]:
    """Auto-assign /Scope on TH struct elements in PDF tables (in-place).

    Heuristic applied per Table:
      - TH in the first TR (row 0) and col 0 -> Scope = Both
      - TH in the first TR (row 0) and col > 0 -> Scope = Column
      - TH in a subsequent TR and col 0       -> Scope = Row
      - TH elsewhere                          -> Scope = Column (default)

    Skips TH elements that already have a /Scope set to avoid double-patching.

    Returns {ok, tables_found, repairs_made}.
    """
    try:
        import pikepdf
    except ImportError:
        return {"ok": False, "error": "pikepdf not installed"}

    try:
        pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    TABLE_TAGS   = {"/Table"}
    ROW_TAGS     = {"/TR"}
    SECTION_TAGS = {"/THead", "/TBody", "/TFoot"}
    CELL_TAGS    = {"/TH", "/TD"}

    def _get_tag(obj: Any) -> str:
        try:
            return str(obj.get("/S", ""))
        except Exception:
            return ""

    def _children(obj: Any) -> list:
        """Return direct children from /K as a flat list of resolved objects."""
        try:
            k = obj.get("/K")
        except Exception:
            return []
        if k is None:
            return []
        if isinstance(k, pikepdf.Array):
            out = []
            for child in k:
                try:
                    out.append(child.obj if hasattr(child, "obj") else child)
                except Exception:
                    pass
            return out
        if isinstance(k, pikepdf.Dictionary):
            return [k]
        if hasattr(k, "obj"):
            return [k.obj]
        return []

    def _get_scope(obj: Any) -> str | None:
        """Return existing /Scope value or None."""
        try:
            a = obj.get("/A")
            if a is None:
                return None
            candidates = list(a) if isinstance(a, pikepdf.Array) else [a]
            for attr in candidates:
                try:
                    scope = attr.get("/Scope")
                    if scope is not None:
                        return str(scope)
                except Exception:
                    pass
        except Exception:
            pass
        return None

    def _set_scope(obj: Any, scope: str) -> bool:
        """Set /Scope on obj/A (create attribute dict if needed). Returns True on success."""
        try:
            a = obj.get("/A")
            if a is None:
                obj["/A"] = pikepdf.Dictionary(**{
                    "/O": pikepdf.Name("/Table"),
                    "/Scope": pikepdf.Name(f"/{scope}"),
                })
                return True
            if isinstance(a, pikepdf.Array):
                # Find an existing Table-owner attr dict, or append a new one.
                for attr in a:
                    try:
                        if str(attr.get("/O", "")) == "/Table":
                            attr["/Scope"] = pikepdf.Name(f"/{scope}")
                            return True
                    except Exception:
                        pass
                # No Table-owner attr found — append one.
                a.append(pikepdf.Dictionary(**{
                    "/O": pikepdf.Name("/Table"),
                    "/Scope": pikepdf.Name(f"/{scope}"),
                }))
                return True
            # Single attr dict.
            a["/Scope"] = pikepdf.Name(f"/{scope}")
            return True
        except Exception as _e:
            log.debug("_set_scope failed: %s", _e)
            return False

    def _collect_rows(table_obj: Any) -> list[list[Any]]:
        """Return list-of-rows; each row is a list of cell (TH/TD) objects."""
        rows: list[list[Any]] = []
        def _visit(obj: Any, depth: int = 0) -> None:
            if depth > 10:
                return
            tag = _get_tag(obj)
            if tag in ROW_TAGS:
                row = [c for c in _children(obj) if _get_tag(c) in CELL_TAGS]
                if row:
                    rows.append(row)
                return
            if tag in SECTION_TAGS or tag in TABLE_TAGS:
                for child in _children(obj):
                    _visit(child, depth + 1)
        _visit(table_obj)
        return rows

    # Walk the structure tree to find all Table elements.
    tables: list[Any] = []

    def _find_tables(obj: Any, depth: int = 0) -> None:
        if depth > 60:
            return
        try:
            tag = _get_tag(obj)
            if tag in TABLE_TAGS:
                tables.append(obj)
                return  # Don't recurse into nested tables for now.
            for child in _children(obj):
                _find_tables(child, depth + 1)
        except Exception:
            pass

    try:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            pdf.close()
            return {"ok": True, "tables_found": 0, "repairs_made": 0, "note": "No structure tree"}
        _find_tables(struct_root)
    except Exception as exc:
        pdf.close()
        return {"ok": False, "error": f"Structure tree walk failed: {exc}"}

    repairs = 0
    for table in tables:
        rows = _collect_rows(table)
        for row_idx, row in enumerate(rows):
            for col_idx, cell in enumerate(row):
                if _get_tag(cell) != "/TH":
                    continue
                if _get_scope(cell) is not None:
                    continue  # Already has a scope — leave it alone.
                if row_idx == 0 and col_idx == 0:
                    scope = "Both"
                elif row_idx == 0:
                    scope = "Column"
                else:
                    scope = "Row"
                if _set_scope(cell, scope):
                    repairs += 1

    if repairs > 0:
        fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(pdf_path))
        os.close(fd)
        try:
            pdf.save(tmp)
            os.replace(tmp, pdf_path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            pdf.close()
            raise
    pdf.close()

    log.info("patch_table_headers: %d repair(s) across %d table(s) in %s",
             repairs, len(tables), pdf_path)
    return {"ok": True, "tables_found": len(tables), "repairs_made": repairs}
