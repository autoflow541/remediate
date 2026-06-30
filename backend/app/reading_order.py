"""Reading order extraction and rewrite for the PDF remediation studio.

Two public functions:

    extract_reading_order(pdf_path) -> list[dict]
        Walk the PDF structure tree in document order and return a flat list
        of structural elements (headings, paragraphs, figures, tables, lists,
        etc.).  Each element carries a stable ``id`` that can be passed back
        to apply_reading_order to identify elements by position.

    apply_reading_order(pdf_path, ordered_ids) -> dict
        Given a list of element IDs in the desired new order, rewrite the
        StructTreeRoot so that the top-level children of the Document element
        appear in that order.  Returns {ok, changes_made}.

Design notes
------------
* We operate on the *top-level* children of the Document struct element.
  This is the level at which reading-order problems typically occur (a
  sidebar after the main content, a figure before its section, etc.).
* Nested content (the children of a section/heading) is preserved verbatim
  — we only move whole subtrees, not individual MCIDs.
* Atomic save via tempfile + os.replace so corruption on failure is impossible.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

log = logging.getLogger(__name__)

# Struct types we surface in the UI (everything else is shown as "Other")
_HEADING_TAGS = {"/H", "/H1", "/H2", "/H3", "/H4", "/H5", "/H6"}
_KNOWN_TAGS = _HEADING_TAGS | {
    "/P", "/Figure", "/Table", "/L", "/LI", "/Link",
    "/Sect", "/Div", "/Art", "/BlockQuote", "/Caption",
    "/TOC", "/TOCI", "/Formula", "/Form", "/Note",
}

_TAG_LABEL = {
    "/H": "Heading", "/H1": "H1", "/H2": "H2", "/H3": "H3",
    "/H4": "H4", "/H5": "H5", "/H6": "H6",
    "/P": "Paragraph", "/Figure": "Figure", "/Table": "Table",
    "/L": "List", "/LI": "List item", "/Link": "Link",
    "/Sect": "Section", "/Div": "Div", "/Art": "Article",
    "/BlockQuote": "Block quote", "/Caption": "Caption",
    "/TOC": "TOC", "/TOCI": "TOC item", "/Formula": "Formula",
    "/Form": "Form", "/Note": "Note",
}


def _pikepdf():
    try:
        import pikepdf
        return pikepdf
    except ImportError:
        return None


def _str_tag(obj: Any) -> str:
    try:
        v = obj.get("/S")
        return str(v) if v is not None else ""
    except Exception:
        return ""


def _text_preview(obj: Any, pikepdf_mod: Any, max_len: int = 80) -> str:
    """Best-effort text extraction from a struct element."""
    # Try /Alt first (figures)
    try:
        alt = obj.get("/Alt")
        if alt:
            return str(alt)[:max_len]
    except Exception:
        pass

    # Recursively collect text from ActualText and then from MCID-referenced
    # content streams.  We don't parse content streams here — just look for
    # /ActualText on any descendant.
    buf: list[str] = []

    def _collect(o: Any, depth: int = 0) -> None:
        if depth > 8 or len(buf) > 3:
            return
        try:
            at = o.get("/ActualText")
            if at:
                buf.append(str(at).strip())
                return
        except Exception:
            pass
        try:
            k = o.get("/K")
            if k is None:
                return
            if isinstance(k, pikepdf_mod.Array):
                for child in k:
                    try:
                        if isinstance(child, pikepdf_mod.Dictionary):
                            _collect(child, depth + 1)
                        elif hasattr(child, "obj"):
                            _collect(child.obj, depth + 1)
                    except Exception:
                        pass
            elif isinstance(k, pikepdf_mod.Dictionary):
                _collect(k, depth + 1)
            elif hasattr(k, "obj"):
                _collect(k.obj, depth + 1)
        except Exception:
            pass

    _collect(obj)
    return " ".join(buf)[:max_len] if buf else ""


def _page_number(obj: Any) -> int | None:
    """Try to read /Pg page index from a struct element or its first child."""
    try:
        pg = obj.get("/Pg")
        if pg is not None:
            return None  # We can't resolve indirect refs to page numbers easily
    except Exception:
        pass
    return None


def extract_reading_order(pdf_path: str) -> list[dict]:
    """Return structural elements in current structure-tree order.

    Each entry::

        {
            "id":      str,    # stable positional ID: "e0", "e1", …
            "type":    str,    # human label: "H1", "Paragraph", "Figure", …
            "tag":     str,    # raw /S value: "/H1", "/P", …
            "preview": str,    # text snippet (may be empty)
            "level":   int,    # nesting depth within Document root (0 = top)
            "children": int,   # number of direct struct children
        }

    Only the top-level children of the Document struct element are returned
    (depth 0).  This is the granularity at which reading order is edited.
    If the tree has no Document element, all root-level /K children are used.
    """
    pk = _pikepdf()
    if pk is None:
        return []

    try:
        pdf = pk.open(pdf_path)
    except Exception as exc:
        log.warning("extract_reading_order: cannot open %s: %s", pdf_path, exc)
        return []

    try:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            return []

        # Find the Document root element (or fall back to StructTreeRoot itself)
        doc_elem = None
        k_root = struct_root.get("/K")
        if k_root is not None:
            if isinstance(k_root, pk.Array):
                for child in k_root:
                    try:
                        c = child.obj if hasattr(child, "obj") else child
                        if str(c.get("/S", "")) == "/Document":
                            doc_elem = c
                            break
                    except Exception:
                        pass
                if doc_elem is None:
                    doc_elem = struct_root  # use root directly
            elif isinstance(k_root, pk.Dictionary):
                if str(k_root.get("/S", "")) == "/Document":
                    doc_elem = k_root
                else:
                    doc_elem = struct_root
            elif hasattr(k_root, "obj"):
                o = k_root.obj
                if str(o.get("/S", "")) == "/Document":
                    doc_elem = o
                else:
                    doc_elem = struct_root
        else:
            doc_elem = struct_root

        # Get the top-level children
        children_k = doc_elem.get("/K") if doc_elem is not struct_root else struct_root.get("/K")
        if children_k is None:
            return []

        if not isinstance(children_k, pk.Array):
            # Single child
            items = [children_k]
        else:
            items = list(children_k)

        elements: list[dict] = []
        for idx, item in enumerate(items):
            try:
                obj = item.obj if hasattr(item, "obj") else item
                if not isinstance(obj, pk.Dictionary):
                    continue
                tag = _str_tag(obj)
                label = _TAG_LABEL.get(tag, "Other" if tag else "MCID")
                if not tag:
                    continue  # skip raw MCID integers at top level

                # Count struct children
                k = obj.get("/K")
                n_children = 0
                if isinstance(k, pk.Array):
                    n_children = sum(
                        1 for c in k
                        if isinstance(
                            c.obj if hasattr(c, "obj") else c,
                            pk.Dictionary,
                        )
                    )
                elif isinstance(k, pk.Dictionary):
                    n_children = 1

                elements.append({
                    "id": f"e{idx}",
                    "type": label,
                    "tag": tag,
                    "preview": _text_preview(obj, pk),
                    "level": 0,
                    "children": n_children,
                })
            except Exception as exc:
                log.debug("extract_reading_order: skipping item %d: %s", idx, exc)
                continue

        return elements

    except Exception as exc:
        log.warning("extract_reading_order failed: %s", exc)
        return []
    finally:
        try:
            pdf.close()
        except Exception:
            pass


def apply_reading_order(pdf_path: str, ordered_ids: list[str]) -> dict:
    """Rewrite the Document struct element's /K array to match ordered_ids.

    ordered_ids is a list of "e<N>" strings as returned by extract_reading_order.
    Elements not mentioned in ordered_ids are appended at the end (in their
    original relative order) so nothing is lost.

    Returns {ok: bool, changes_made: int, error?: str}.
    """
    pk = _pikepdf()
    if pk is None:
        return {"ok": False, "error": "pikepdf not installed"}

    try:
        pdf = pk.open(pdf_path, allow_overwriting_input=True)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    try:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            pdf.close()
            return {"ok": True, "changes_made": 0, "note": "No structure tree"}

        # Locate Document element and its parent container
        doc_elem = None
        parent_container = None  # the object whose /K we will rewrite
        k_root = struct_root.get("/K")

        if isinstance(k_root, pk.Array):
            for child in k_root:
                try:
                    c = child.obj if hasattr(child, "obj") else child
                    if str(c.get("/S", "")) == "/Document":
                        doc_elem = c
                        parent_container = doc_elem
                        break
                except Exception:
                    pass
        elif k_root is not None:
            obj = k_root.obj if hasattr(k_root, "obj") else k_root
            if str(obj.get("/S", "")) == "/Document":
                doc_elem = obj
                parent_container = doc_elem

        if parent_container is None:
            # Fall back: reorder StructTreeRoot /K directly
            parent_container = struct_root

        current_k = parent_container.get("/K")
        if current_k is None:
            pdf.close()
            return {"ok": True, "changes_made": 0}

        if not isinstance(current_k, pk.Array):
            # Single item — nothing to reorder
            pdf.close()
            return {"ok": True, "changes_made": 0}

        # Separate struct dict items from raw MCID integers (keep integers in place)
        struct_items: list[tuple[int, Any]] = []   # (original_index, obj)
        non_struct: list[tuple[int, Any]] = []

        for i, item in enumerate(current_k):
            try:
                obj = item.obj if hasattr(item, "obj") else item
                if isinstance(obj, pk.Dictionary) and obj.get("/S") is not None:
                    struct_items.append((i, item))
                else:
                    non_struct.append((i, item))
            except Exception:
                non_struct.append((i, item))

        if not struct_items:
            pdf.close()
            return {"ok": True, "changes_made": 0}

        # Build id->item map
        id_map: dict[str, Any] = {}
        for idx, (orig_i, item) in enumerate(struct_items):
            id_map[f"e{orig_i}"] = item

        # Build new order: requested IDs first, then any not mentioned
        requested = [id_map[eid] for eid in ordered_ids if eid in id_map]
        mentioned = set(ordered_ids)
        remainder = [item for eid, item in
                     [(f"e{i}", it) for i, it in struct_items]
                     if eid not in mentioned]
        new_struct_order = requested + remainder

        # Merge back with non-struct items (keep them at their original positions)
        new_k_list: list[Any] = []
        ns_iter = iter(non_struct)
        ns_next = next(ns_iter, None)
        new_struct_iter = iter(new_struct_order)

        # Simple strategy: interleave non-struct items at original positions
        total = len(current_k)
        struct_out = list(new_struct_order)
        ns_dict = dict(non_struct)

        rebuilt: list[Any] = []
        s_idx = 0
        for i in range(total):
            if i in ns_dict:
                rebuilt.append(ns_dict[i])
            else:
                if s_idx < len(struct_out):
                    rebuilt.append(struct_out[s_idx])
                    s_idx += 1

        # Check if order actually changed
        original_order = [id_map.get(f"e{i}") for i, _ in struct_items]
        changes_made = sum(
            1 for a, b in zip(original_order, new_struct_order)
            if a is not b
        )

        if changes_made == 0:
            pdf.close()
            return {"ok": True, "changes_made": 0}

        parent_container["/K"] = pk.Array(rebuilt)

        # Atomic save
        fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(pdf_path))
        os.close(fd)
        try:
            pdf.save(tmp)
            os.replace(tmp, pdf_path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

        log.info("apply_reading_order: %d element(s) reordered in %s", changes_made, pdf_path)
        return {"ok": True, "changes_made": changes_made}

    except Exception as exc:
        log.warning("apply_reading_order failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            pdf.close()
        except Exception:
            pass
