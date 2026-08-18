"""Reading order extraction and rewrite for the PDF remediation studio.

Two public functions:

    extract_reading_order(pdf_path, max_depth=None) -> list[dict]
        Walk the PDF structure tree in document (pre-order) order and return a
        flat list of structural elements (headings, paragraphs, figures,
        tables, lists, …).  Each element carries a stable ``id`` and a
        ``parent_id`` so a caller — the reorder UI or the AI reading-order
        pass — can see and reorder elements at *any* depth, not just the top.

    apply_reading_order(pdf_path, ordered_ids) -> dict
        Given a list of element IDs in the desired order, rewrite the structure
        tree so that, within every parent, the children appear in that order.
        Returns {ok, changes_made}.

Design notes
------------
* Elements are reordered **within their parent** (siblings), at any level.
  A node is never moved to a different parent — that would change the
  document's semantics, not just its reading order.
* Non-structural /K entries (raw MCID integers, OBJR annotation refs) are kept
  at their original positions; only struct-element siblings are permuted.
* IDs are a stable path within one extract ("e0", "e1", "e1_0", …).  Top-level
  IDs are "e0", "e1", … — identical to the previous top-level-only scheme, so
  older callers keep working.
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


def _resolve(item: Any) -> Any:
    """Resolve a /K entry (which may be an indirect ref wrapper) to its object."""
    return item.obj if hasattr(item, "obj") else item


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


def _document_container(struct_root: Any, pk: Any) -> Any:
    """Return the element whose /K holds the top-level reading sequence: the
    /Document element if present, else the StructTreeRoot itself."""
    try:
        k_root = struct_root.get("/K")
    except Exception:
        return struct_root
    if k_root is None:
        return struct_root
    items = list(k_root) if isinstance(k_root, pk.Array) else [k_root]
    for child in items:
        try:
            c = _resolve(child)
            if isinstance(c, pk.Dictionary) and str(c.get("/S", "")) == "/Document":
                return c
        except Exception:
            pass
    return struct_root


def _direct_struct_children(obj: Any, pk: Any):
    """Yield the direct struct-element children of *obj* (skipping MCID/OBJR)."""
    try:
        k = obj.get("/K")
    except Exception:
        return
    if k is None:
        return
    items = list(k) if isinstance(k, pk.Array) else [k]
    for it in items:
        try:
            c = _resolve(it)
            if isinstance(c, pk.Dictionary) and c.get("/S") is not None:
                yield c
        except Exception:
            pass


def _walk_tree(container: Any, pk: Any, prefix: str = "e", depth: int = 0,
               parent_id: str | None = None, seen: set | None = None):
    """Yield every struct element under *container* in pre-order.

    Each yield is a tuple::

        (elem_id, obj, item, parent_obj, pos, depth, parent_id)

    where ``elem_id`` is a stable path id within this walk ("e0", "e1_0", …),
    ``item`` is the original /K entry (used to rewrite /K), ``parent_obj`` is
    the container whose /K holds this element, and ``pos`` is the element's
    index in that /K array.
    """
    if seen is None:
        seen = set()
    try:
        k = container.get("/K")
    except Exception:
        return
    if k is None:
        return
    items = list(k) if isinstance(k, pk.Array) else [k]
    for pos, item in enumerate(items):
        try:
            obj = _resolve(item)
        except Exception:
            continue
        if not isinstance(obj, pk.Dictionary) or obj.get("/S") is None:
            continue
        try:
            og = obj.objgen
            if og != (0, 0):
                if og in seen:
                    continue
                seen.add(og)
        except Exception:
            pass
        elem_id = f"{prefix}{pos}"
        yield (elem_id, obj, item, container, pos, depth, parent_id)
        yield from _walk_tree(obj, pk, prefix=elem_id + "_", depth=depth + 1,
                              parent_id=elem_id, seen=seen)


def extract_reading_order(pdf_path: str, max_depth: int | None = None) -> list[dict]:
    """Return structural elements in current structure-tree (pre-order) order.

    Each entry::

        {
            "id":        str,        # stable path ID: "e0", "e1", "e1_0", …
            "parent_id": str | None, # ID of the containing element (None = top)
            "type":      str,        # human label: "H1", "Paragraph", …
            "tag":       str,        # raw /S value: "/H1", "/P", …
            "preview":   str,        # text snippet (recovered from MCID content)
            "level":     int,        # nesting depth (0 = top-level)
            "children":  int,        # number of direct struct children
        }

    ``max_depth`` limits how deep the walk descends (None = the whole tree;
    0 = top-level only, the historical behaviour).  Elements are returned in
    document order, so the list *is* the current reading order.
    """
    pk = _pikepdf()
    if pk is None:
        return []

    try:
        pdf = pk.open(pdf_path)
    except Exception as exc:
        log.warning("extract_reading_order: cannot open %s: %s", pdf_path, exc)
        return []

    # page objgen -> pikepdf Page, so a struct element's MCID text can be
    # recovered from its page's content stream (parsed once, cached).
    page_by_objgen: dict = {}
    try:
        for page in pdf.pages:
            page_by_objgen[page.obj.objgen] = page
    except Exception:
        pass
    _mcid_cache: dict = {}

    def _mcid_preview(obj, max_len: int = 80) -> str:
        from .mc_text import page_mcid_texts, element_mcids
        try:
            pg = obj.get("/Pg")
            if pg is None:
                return ""
            og = pg.objgen
        except Exception:
            return ""
        page = page_by_objgen.get(og)
        if page is None:
            return ""
        if og not in _mcid_cache:
            _mcid_cache[og] = page_mcid_texts(page)
        texts = _mcid_cache[og]
        return " ".join(texts.get(m, "") for m in element_mcids(obj)).strip()[:max_len]

    try:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            return []

        container = _document_container(struct_root, pk)

        elements: list[dict] = []
        for elem_id, obj, _item, _parent, _pos, depth, parent_id in _walk_tree(container, pk):
            if max_depth is not None and depth > max_depth:
                continue
            try:
                tag = _str_tag(obj)
                if not tag:
                    continue
                label = _TAG_LABEL.get(tag, "Other")
                n_children = sum(1 for _ in _direct_struct_children(obj, pk))
                preview = _text_preview(obj, pk) or _mcid_preview(obj)
                elements.append({
                    "id": elem_id,
                    "parent_id": parent_id,
                    "type": label,
                    "tag": tag,
                    "preview": preview,
                    "level": depth,
                    "children": n_children,
                })
            except Exception as exc:
                log.debug("extract_reading_order: skipping %s: %s", elem_id, exc)
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
    """Rewrite the structure tree so siblings appear in the requested order.

    ``ordered_ids`` is a list of IDs (from extract_reading_order) in the desired
    reading order — it may span multiple levels.  Within each parent, its struct
    children are permuted to match their relative order in ``ordered_ids``;
    children not mentioned keep their original relative order and are appended.
    Elements are only reordered among their own siblings, never moved between
    parents.

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

        container = _document_container(struct_root, pk)

        # Group every struct element by its parent container object.
        # parent objgen -> {"obj": parent, "children": [(pos, elem_id, item)]}
        parents: dict[Any, dict] = {}
        parent_order: list[Any] = []
        for elem_id, _obj, item, parent, pos, _depth, _pid in _walk_tree(container, pk):
            try:
                pog = parent.objgen
            except Exception:
                pog = id(parent)
            entry = parents.get(pog)
            if entry is None:
                entry = {"obj": parent, "children": []}
                parents[pog] = entry
                parent_order.append(pog)
            entry["children"].append((pos, elem_id, item))

        if not parents:
            pdf.close()
            return {"ok": True, "changes_made": 0}

        rank = {eid: i for i, eid in enumerate(ordered_ids)}
        total_changes = 0

        for pog in parent_order:
            entry = parents[pog]
            parent_obj = entry["obj"]
            kids = sorted(entry["children"], key=lambda t: t[0])  # by original pos
            orig_ids = [eid for _, eid, _ in kids]
            id_item = {eid: item for _, eid, item in kids}
            positions = [pos for pos, _, _ in kids]

            mentioned = [eid for eid in orig_ids if eid in rank]
            unmentioned = [eid for eid in orig_ids if eid not in rank]
            new_ids = sorted(mentioned, key=lambda e: rank[e]) + unmentioned
            if new_ids == orig_ids:
                continue

            # Rebuild this parent's /K: struct slots take the new order;
            # everything else (MCID ints, OBJR, …) stays where it was.
            try:
                k = parent_obj.get("/K")
            except Exception:
                continue
            items = list(k) if isinstance(k, pk.Array) else [k]
            pos_set = set(positions)
            new_iter = iter(new_ids)
            rebuilt: list[Any] = []
            for pos, it in enumerate(items):
                if pos in pos_set:
                    rebuilt.append(id_item[next(new_iter)])
                else:
                    rebuilt.append(it)
            parent_obj["/K"] = pk.Array(rebuilt)
            total_changes += sum(1 for a, b in zip(orig_ids, new_ids) if a != b)

        if total_changes == 0:
            pdf.close()
            return {"ok": True, "changes_made": 0}

        # Atomic save
        fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(pdf_path) or None)
        os.close(fd)
        try:
            pdf.save(tmp)
            os.replace(tmp, pdf_path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

        log.info("apply_reading_order: %d element(s) reordered in %s", total_changes, pdf_path)
        return {"ok": True, "changes_made": total_changes}

    except Exception as exc:
        log.warning("apply_reading_order failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            pdf.close()
        except Exception:
            pass
