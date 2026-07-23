"""orphan_mcid_fix.py — adopt marked content no structure element references.

Real-world tagged PDFs frequently contain BDC groups with MCIDs that no
structure element points to (the producer pruned elements without cleaning the
stream). veraPDF 7.1-3 fails each one: the content is neither an artifact nor
validly tagged.

Hiding such content as /Artifact would remove real text from assistive tech,
so instead each orphaned MCID is *adopted*: a P structure element is created
under the tree's document root with /Pg + K=mcid, and the page's ParentTree
entry is extended so the MCID resolves. Content, existing tags and reading
order are untouched; the adopted paragraphs append after existing children.

Conservative: pages whose ParentTree entry uses a nested number tree (/Kids)
are skipped rather than risk corrupting the tree.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _stream_mcids(pdf, page) -> set[int]:
    """All MCIDs present in a page's content stream."""
    import pikepdf

    found: set[int] = set()
    try:
        for instr in pikepdf.parse_content_stream(page):
            if str(instr.operator) == "BDC" and len(instr.operands) == 2:
                try:
                    # Skip /Artifact sequences: an artifact that carries an MCID
                    # is intentionally NOT tagged content. Adopting it as a
                    # structure element makes it both artifact and tagged, which
                    # fails veraPDF 7.1-1 AND 7.1-2 on the same content.
                    if str(instr.operands[0]) == "/Artifact":
                        continue
                    props = instr.operands[1]
                    mcid = props.get("/MCID") if hasattr(props, "get") else None
                    if mcid is not None:
                        found.add(int(mcid))
                except Exception:
                    continue
    except Exception as exc:
        log.debug("orphan_mcid stream scan: %s", exc)
    return found


def _nums_entry(struct_root, key: int):
    """Find (nums_array, index_of_value) for `key` in a flat /Nums parent tree.
    Returns (None, None) when absent or when the tree uses /Kids (unsupported)."""
    pt = struct_root.get("/ParentTree")
    if pt is None:
        return None, None
    if pt.get("/Kids") is not None:
        return "kids", None
    nums = pt.get("/Nums")
    if nums is None:
        return None, None
    for i in range(0, len(nums) - 1, 2):
        try:
            if int(nums[i]) == key:
                return nums, i + 1
        except Exception:
            continue
    return nums, None


def _reachable(struct_root, pikepdf) -> set:
    """Objgens of every element reachable from the StructTreeRoot via /K."""
    reach: set = set()
    stack = [struct_root]
    while stack:
        n = stack.pop()
        try:
            og = n.objgen
            if og != (0, 0):
                if og in reach:
                    continue
                reach.add(og)
        except Exception:
            pass
        try:
            k = n.get("/K")
        except Exception:
            continue
        kids = list(k) if isinstance(k, pikepdf.Array) else ([k] if k is not None else [])
        for kid in kids:
            if hasattr(kid, "get"):
                stack.append(kid)
    return reach


# Stray elements of these types need a wrapper to keep nesting valid when
# reattached at document level.
_WRAP_AS = {"LI": "L", "LBody": "L", "TR": "Table", "TD": "Table", "TH": "Table"}


def adopt_orphaned_mcids(pdf_path: str) -> tuple[int, list[str]]:
    """Reconnect content that veraPDF sees as untagged (PDF/UA 7.1-3).

    Two cases, both found in real documents:
      1. MCIDs present in a page stream with no ParentTree entry at all —
         adopted as new P elements.
      2. ParentTree entries pointing at *disconnected* struct elements (their
         /P chain never reaches the root — orphaned islands, typically list
         items whose parent list was pruned). The island's topmost node is
         reattached under the document element, wrapped (LI -> L, TR -> Table)
         so structure-type nesting stays valid.

    Returns (fix_count, notes).
    """
    try:
        import pikepdf
        from pikepdf import Array, Dictionary, Name
    except ImportError:
        return 0, []

    adopted = 0
    reattached = 0
    skipped_kids = False
    try:
        with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
            struct_root = pdf.Root.get("/StructTreeRoot")
            if struct_root is None:
                return 0, []
            root_kids = struct_root.get("/K")
            if root_kids is None:
                return 0, []
            doc_elem = root_kids[0] if isinstance(root_kids, pikepdf.Array) else root_kids
            reach = _reachable(struct_root, pikepdf)

            def attach(elem) -> None:
                """Append elem to the document element's kids and set /P."""
                k = doc_elem.get("/K")
                if isinstance(k, pikepdf.Array):
                    k.append(elem)
                elif k is not None:
                    doc_elem[Name("/K")] = Array([k, elem])
                else:
                    doc_elem[Name("/K")] = Array([elem])
                elem[Name("/P")] = doc_elem

            handled_islands: set = set()

            for page in pdf.pages:
                sp = page.obj.get("/StructParents")
                if sp is None:
                    continue
                nums, vi = _nums_entry(struct_root, int(sp))
                if nums == "kids":
                    skipped_kids = True
                    continue
                if nums is None or vi is None:
                    continue
                try:
                    page_parents = nums[vi]
                except Exception:
                    continue

                in_stream = _stream_mcids(pdf, page)

                # ── case 2: referenced but disconnected elements ──────────────
                for m in sorted(in_stream):
                    if m >= len(page_parents):
                        continue
                    try:
                        e = page_parents[m]
                        if e is None or not hasattr(e, "get"):
                            continue
                        og = e.objgen
                    except Exception:
                        continue
                    if og in reach:
                        continue
                    # climb to the island's topmost node
                    top = e
                    for _ in range(32):
                        p = top.get("/P")
                        if p is None or not hasattr(p, "get"):
                            break
                        try:
                            if p.objgen in reach or p.get("/Type") == Name.StructTreeRoot:
                                break
                        except Exception:
                            break
                        top = p
                    try:
                        top_og = top.objgen
                    except Exception:
                        top_og = None
                    if top_og in handled_islands:
                        continue
                    tag = str(top.get("/S", "")).lstrip("/")
                    wrapper_tag = _WRAP_AS.get(tag)
                    if wrapper_tag:
                        wrapper = pdf.make_indirect(Dictionary(
                            Type=Name.StructElem, S=Name("/" + wrapper_tag),
                            P=doc_elem, K=Array([top]),
                        ))
                        top[Name("/P")] = wrapper
                        attach_target = wrapper
                    else:
                        attach_target = top
                    attach(attach_target)
                    if top_og is not None:
                        handled_islands.add(top_og)
                    # mark whole island reachable so we don't re-handle it
                    reach |= _reachable(attach_target, pikepdf)
                    reattached += 1

                # ── case 1: MCIDs with no ParentTree entry at all ─────────────
                orphans = []
                for m in sorted(in_stream):
                    absent = m >= len(page_parents)
                    if not absent:
                        try:
                            v = page_parents[m]
                            absent = v is None or (not hasattr(v, "get") and str(v) == "null")
                        except Exception:
                            absent = True
                    if absent:
                        orphans.append(m)
                if orphans:
                    while len(page_parents) < max(orphans) + 1:
                        page_parents.append(None)
                    for m in orphans:
                        elem = pdf.make_indirect(Dictionary(
                            Type=Name.StructElem, S=Name.P, P=doc_elem,
                            Pg=page.obj, K=m,
                        ))
                        page_parents[m] = elem
                        attach(elem)
                        adopted += 1

            if adopted or reattached:
                pdf.save()
    except Exception as exc:
        log.warning("orphan_mcid_fix: %s", exc)
        return 0, []

    notes = []
    if reattached:
        notes.append(f"Reconnected {reattached} orphaned structure subtree"
                     f"{'s' if reattached != 1 else ''} (disconnected list/table "
                     "islands) to the document (PDF/UA 7.1-3)")
    if adopted:
        notes.append(f"Adopted {adopted} unreferenced marked-content region"
                     f"{'s' if adopted != 1 else ''} as paragraphs (PDF/UA 7.1-3)")
    if skipped_kids:
        notes.append("Some pages use a nested ParentTree (/Kids) — left untouched")
    return adopted + reattached, notes
