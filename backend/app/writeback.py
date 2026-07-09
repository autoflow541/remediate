"""pikepdf structure-tree writer.  [Phase 3]

Fuses the original PDF with the studio's remediation manifest and writes a
PDF/UA-oriented tagged file using pikepdf (MPL-2.0).

What it writes:
  - reading order   = manifest node order (pre-order traversal of the tree)
  - a real structure tree: StructTreeRoot -> Document -> H1..H6 / P / Table /
    TR / TH|TD / L / LI / Figure / Caption ...
  - marked content: each page's content stream is re-marked so every text run is
    bound to its structure element via an MCID (located by OpenDataLoader's
    bounding boxes), and everything else is tagged /Artifact
  - a ParentTree (number tree) mapping each page's MCIDs back to struct elements
  - Figure /Alt from the manifest; decorative images -> /Artifact (not in tree)
  - table header cells -> /TH with a /Scope attribute
  - Title -> Info dict + XMP; language -> /Lang
  - /MarkInfo /Marked true, /ViewerPreferences /DisplayDocTitle true, and the
    PDF/UA-1 identifier in XMP so veraPDF recognises the conformance claim

The genuinely hard part (per the project plan) is binding each manifest node to
the real marked-content sequence on its page. We do it by tracking the text /
CTM matrices through the content stream and testing each painting operator's
origin against the node bounding boxes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Name, Operator, String


class WritebackError(RuntimeError):
    """The manifest could not be written back into the PDF."""


# Structure tags that are containers (no direct marked content of their own).
CONTAINER_TAGS = {"Table", "TR", "TD", "TH", "L", "LI", "TOC", "TOCI", "Document"}
HEADING_TAGS = {f"H{i}" for i in range(1, 7)}


# --------------------------------------------------------------------------
# 2x3 affine matrix helpers ([a, b, c, d, e, f]) for tracking text position.
# --------------------------------------------------------------------------
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mat_mul(m1: tuple, m2: tuple) -> tuple:
    """Return m1 x m2 (apply m1 first, then m2 — PDF convention)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _apply(m: tuple, x: float, y: float) -> tuple:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


# --------------------------------------------------------------------------
# Leaf collection: flatten the manifest tree into struct elements.
# --------------------------------------------------------------------------
@dataclass
class Leaf:
    """A manifest node that owns marked content on a page (text or figure)."""

    node: dict
    elem: Any  # the pikepdf indirect StructElem
    page: int
    bbox: list[float] | None
    decorative: bool
    mcid: int | None = None


@dataclass
class _BuildState:
    leaves: list[Leaf] = field(default_factory=list)


def _to_array(bbox) -> list[float] | None:
    if not bbox:
        return None
    try:
        return [float(v) for v in bbox]
    except (TypeError, ValueError):
        return None


def _bbox_area(b: list[float]) -> float:
    return abs((b[2] - b[0]) * (b[3] - b[1]))


def _contains(b: list[float], x: float, y: float, tol: float = 4.0) -> bool:
    return (b[0] - tol) <= x <= (b[2] + tol) and (b[1] - tol) <= y <= (b[3] + tol)


class StructTreeBuilder:
    """Builds the structure tree and the page content re-marking for a PDF."""

    def __init__(self, pdf: pikepdf.Pdf, manifest: dict) -> None:
        self.pdf = pdf
        self.manifest = manifest
        self.state = _BuildState()
        self.struct_root = None
        self._id_elems: dict[str, Any] = {}  # header cell /ID -> struct elem
        self.report = {
            "elements": 0,
            "figures": 0,
            "artifacts_decorative": 0,
            "headers": 0,
            "pages_remarked": 0,
            "mcids": 0,
            "unbound_leaves": 0,
            "bookmarks": 0,
            "links_tagged": 0,
        }

    # -- struct tree construction -------------------------------------------
    def _table_cell_attrs(self, node: dict, tag: str):
        """Build the /A Table attribute object for a cell, or None if it needs
        no attributes. Carries Scope (TH), RowSpan/ColSpan, and Headers."""
        attr = Dictionary(O=Name.Table)
        used = False
        if tag == "TH":
            scope = node.get("scope") or "Column"
            attr.Scope = Name("/" + str(scope).capitalize())
            used = True
        rs = int(node.get("rowSpan", 1) or 1)
        cs = int(node.get("colSpan", 1) or 1)
        if rs > 1:
            attr.RowSpan = rs
            used = True
        if cs > 1:
            attr.ColSpan = cs
            used = True
        headers = node.get("headers")
        if headers:
            attr.Headers = Array([String(str(h)) for h in headers])
            used = True
        return attr if used else None

    def _new_elem(self, tag: str, parent) -> Any:
        elem = self.pdf.make_indirect(
            Dictionary(Type=Name.StructElem, S=Name("/" + tag), P=parent)
        )
        self.report["elements"] += 1
        return elem

    def _build_node(self, node: dict, parent) -> Any | None:
        tag = node.get("tag", "P")
        children = node.get("children") or []

        # Nodes explicitly marked as artifacts (e.g. running headers/footers
        # detected by header_footer.py, decorative figures, or layout tables
        # identified by ai_analyze as Artifact).  These are not added to the
        # structure tree — their content stream content is instead routed to
        # /Artifact marked content by remark_page().
        if node.get("artifact") or tag == "Artifact" or (node.get("decorative") and tag not in ("LI", "L", "Lbl", "LBody")):
            self.state.leaves.append(
                Leaf(node, None, int(node.get("page", 1)), _to_array(node.get("bbox")),
                     decorative=True)
            )
            self.report["artifacts_decorative"] += 1
            return None

        elem = self._new_elem(tag, parent)

        # Table cells: scope (TH), row/column spans, and Headers/ID associations
        # for complex tables (computed by tables.analyze_tables).
        if tag in ("TH", "TD"):
            attrs = self._table_cell_attrs(node, tag)
            if attrs is not None:
                elem.A = attrs
            header_id = node.get("headerId")
            if header_id:
                elem.ID = String(str(header_id))
                self._id_elems[str(header_id)] = elem
            if tag == "TH":
                self.report["headers"] += 1

        if tag == "Figure":
            # Always write /Alt — veraPDF clause 7.3 requires it even for
            # decorative figures.  Use manifest alt if present, else a generic
            # placeholder that alt_quality.py will flag for human review.
            alt = node.get("alt") or "Figure"
            elem.Alt = String(str(alt))
            self.report["figures"] += 1

        if tag == "Table":
            # Table summary (WCAG 1.3.1 best practice): brief description of
            # table purpose written to /Summary attribute so AT announces it
            # before reading cells.
            summary = node.get("summary", "").strip()
            if summary:
                attr = Dictionary(O=Name.Table, Summary=String(summary))
                existing_a = elem.get("/A")
                if existing_a is not None:
                    try:
                        existing_a.Summary = String(summary)
                    except Exception:
                        elem.A = attr
                else:
                    elem.A = attr

        if tag == "Formula":
            # Write /Alt for screen reader reading of the formula, and
            # /ActualText with the LaTeX representation when available so
            # copy-paste and braille devices get the raw math notation.
            alt = node.get("alt") or "Formula"
            elem.Alt = String(str(alt))
            latex = node.get("latex", "").strip()
            if latex:
                elem.ActualText = String(latex)

        # Language of parts (WCAG 3.1.2): per-element /Lang overrides document lang.
        node_lang = node.get("language")
        if node_lang:
            elem.Lang = String(str(node_lang))

        kid_elems = []
        if children:
            for child in children:
                child_elem = self._build_node(child, elem)
                if child_elem is not None:
                    kid_elems.append(child_elem)
            if kid_elems:
                elem.K = Array(kid_elems)
        else:
            # A leaf that owns marked content (text run or figure XObject).
            self.state.leaves.append(
                Leaf(node, elem, int(node.get("page", 1)), _to_array(node.get("bbox")),
                     decorative=False)
            )
        return elem

    def build_tree(self) -> None:
        self.struct_root = self.pdf.make_indirect(
            Dictionary(Type=Name.StructTreeRoot)
        )
        document = self._new_elem("Document", self.struct_root)
        top_kids = []
        for node in self.manifest.get("nodes", []) or []:
            elem = self._build_node(node, document)
            if elem is not None:
                top_kids.append(elem)
        document.K = Array(top_kids)
        self.struct_root.K = Array([document])

    # -- content re-marking --------------------------------------------------
    def _leaves_for_page(self, page_index: int) -> list[Leaf]:
        # OpenDataLoader page numbers are 1-indexed.
        return [lf for lf in self.state.leaves if lf.page == (page_index + 1)]

    def _match_leaf(self, leaves: list[Leaf], x: float, y: float) -> Leaf | None:
        best: Leaf | None = None
        best_area = None
        for lf in leaves:
            if lf.bbox and _contains(lf.bbox, x, y):
                area = _bbox_area(lf.bbox)
                if best_area is None or area < best_area:
                    best, best_area = lf, area
        return best

    # ------------------------------------------------------------------
    # OCR invisible text layer (scanned pages)
    # ------------------------------------------------------------------

    def _ensure_ocr_font(self, page: pikepdf.Page) -> None:
        """Add /F_OCR (Helvetica) to the page's /Resources if not present."""
        res = page.obj.get("/Resources")
        if res is None:
            res = Dictionary()
            page.obj["/Resources"] = res
        fonts = res.get("/Font")
        if fonts is None:
            fonts = Dictionary()
            res["/Font"] = fonts
        if Name("/F_OCR") not in fonts:
            fonts[Name("/F_OCR")] = Dictionary(
                Type=Name("/Font"),
                Subtype=Name("/Type1"),
                BaseFont=Name("/Helvetica"),
                Encoding=Name("/WinAnsiEncoding"),
            )

    def _inject_ocr_text_layer(self, page: pikepdf.Page, page_ocr: dict) -> None:
        """Append an invisible text stream (Tr 3) for each OCR element.

        This is called *before* remark_page processes the content stream.
        remark_page will then find these text operators by position and bind
        the correct MCIDs and structure tags to them.
        """
        from .ocr_vision import build_ocr_text_stream
        elements = page_ocr.get("elements", [])
        if not elements:
            return
        self._ensure_ocr_font(page)
        stream_bytes = build_ocr_text_stream(elements)
        if not stream_bytes.strip():
            return
        ocr_stream = pikepdf.Stream(self.pdf, stream_bytes)
        existing = page.obj.get("/Contents")
        # /Contents may be a single stream OR an array of streams. Check for
        # the array FIRST: pikepdf's .is_stream raises ValueError ("not a
        # Dictionary or Stream") when called on an Array, so the old
        # stream-first order 500'd on any multi-stream page (e.g. Microsoft
        # Print to PDF output).
        if existing is None:
            page.obj["/Contents"] = ocr_stream
        elif isinstance(existing, pikepdf.Array):
            arr = list(existing)
            arr.append(self.pdf.make_indirect(ocr_stream))
            page.obj["/Contents"] = Array(arr)
        else:
            page.obj["/Contents"] = Array([
                self.pdf.make_indirect(existing),
                self.pdf.make_indirect(ocr_stream),
            ])

    def remark_page(self, page_index: int, page: pikepdf.Page) -> None:
        # --- Scanned page: inject invisible OCR text layer first ---
        _ocr_pages = self.manifest.get("_ocr_pages", {})
        _page_ocr = _ocr_pages.get(str(page_index))
        if _page_ocr and _page_ocr.get("elements"):
            try:
                _has_text = any(
                    str(ins.operator) in {"Tj", "TJ", "'", '"'}
                    for ins in pikepdf.parse_content_stream(page)
                )
            except Exception:
                _has_text = False
            if not _has_text:
                self._inject_ocr_text_layer(page, _page_ocr)

        leaves = self._leaves_for_page(page_index)
        if not leaves:
            return

        try:
            instructions = pikepdf.parse_content_stream(page)
        except Exception:  # pragma: no cover - malformed stream
            return

        # MCID assignment is per page; reuse one MCID per leaf (a struct element
        # may own several content runs that share its MCID).
        page_mcids: dict[int, Leaf] = {}  # mcid -> leaf
        next_mcid = 0

        ctm_stack: list[tuple] = []
        ctm = IDENTITY
        tm = IDENTITY
        tlm = IDENTITY
        leading = 0.0

        out: list = []
        open_group: object = None  # leaf id (int), "artifact", or None

        TEXT_SHOW = {"Tj", "TJ", "'", '"'}
        PATH_OPS = {"m", "l", "c", "v", "y", "re", "h",
                    "S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"}
        BARRIERS = {"BT", "ET", "q", "Q"}

        def emit_op(operands, op_name):
            out.append(pikepdf.ContentStreamInstruction(operands, Operator(op_name)))

        def close_group():
            nonlocal open_group
            if open_group is not None:
                emit_op([], "EMC")
                open_group = None

        def open_leaf(lf: Leaf):
            nonlocal open_group, next_mcid
            if lf.mcid is None:
                lf.mcid = next_mcid
                page_mcids[next_mcid] = lf
                next_mcid += 1
            tag = lf.node.get("tag", "P")
            emit_op([Name("/" + tag), Dictionary(MCID=lf.mcid)], "BDC")
            open_group = id(lf)

        def open_artifact():
            nonlocal open_group
            emit_op([Name.Artifact], "BMC")
            open_group = "artifact"

        for instr in instructions:
            operands = list(instr.operands)
            op = str(instr.operator)

            # --- track matrices ---
            if op == "cm" and len(operands) == 6:
                ctm = _mat_mul(tuple(float(o) for o in operands), ctm)
            elif op == "q":
                ctm_stack.append(ctm)
            elif op == "Q":
                ctm = ctm_stack.pop() if ctm_stack else ctm
            elif op == "BT":
                tm = tlm = IDENTITY
            elif op == "Tm" and len(operands) == 6:
                tm = tlm = tuple(float(o) for o in operands)
            elif op in ("Td", "TD") and len(operands) == 2:
                tx, ty = float(operands[0]), float(operands[1])
                if op == "TD":
                    leading = -ty
                tlm = _mat_mul((1, 0, 0, 1, tx, ty), tlm)
                tm = tlm
            elif op == "TL" and operands:
                leading = float(operands[0])
            elif op == "T*":
                tlm = _mat_mul((1, 0, 0, 1, 0, -leading), tlm)
                tm = tlm

            # --- strip PRE-EXISTING marked-content operators ---
            # Rebuild mode replaces the whole structure tree, so any BMC/BDC/EMC
            # already in the stream (e.g. author-marked /Artifact headers, or
            # MCIDs pointing at the discarded tree) is stale. Left in place they
            # interleave illegally with the groups we emit (veraPDF 7.1-1/7.1-2:
            # tagged content inside Artifact and vice versa). We re-mark every
            # operator ourselves, so dropping them loses nothing.
            if op in ("BMC", "BDC", "EMC"):
                continue

            # --- barriers force-close marked content (keep MC / BT-ET / q-Q
            #     properly nested) ---
            if op in BARRIERS:
                close_group()
                emit_op(operands, op)
                continue

            # --- text-showing operator: bind to a structure element ---
            if op in TEXT_SHOW:
                origin = _apply(_mat_mul(tm, ctm), 0.0, 0.0)
                lf = self._match_leaf(leaves, origin[0], origin[1])
                if lf is not None and not lf.decorative:
                    if open_group != id(lf):
                        close_group()
                        open_leaf(lf)
                else:
                    if open_group != "artifact":
                        close_group()
                        open_artifact()
                emit_op(operands, op)
                continue

            # --- path painting / images -> Artifact (decorative vector) ---
            if op in PATH_OPS or op == "Do" or op == "sh":
                # A figure XObject could match a Figure leaf; otherwise artifact.
                if op == "Do":
                    origin = _apply(ctm, 0.0, 0.0)
                    lf = self._match_leaf(
                        [l for l in leaves if l.node.get("tag") == "Figure"],
                        origin[0], origin[1])
                    if lf is not None and not lf.decorative:
                        if open_group != id(lf):
                            close_group()
                            open_leaf(lf)
                        emit_op(operands, op)
                        continue
                if open_group != "artifact":
                    close_group()
                    open_artifact()
                emit_op(operands, op)
                continue

            # --- neutral op (state / positioning): pass through ---
            emit_op(operands, op)

        close_group()

        new_data = pikepdf.unparse_content_stream(out)
        page.obj.Contents = self.pdf.make_stream(new_data)
        page.obj.StructParents = page_index

        # ParentTree entry for this page: array indexed by MCID.
        if page_mcids:
            self.report["pages_remarked"] += 1
            self.report["mcids"] += len(page_mcids)
            arr = []
            for mcid in range(next_mcid):
                lf = page_mcids.get(mcid)
                if lf is not None and lf.elem is not None:
                    lf.elem.K = mcid
                    lf.elem.Pg = page.obj
                    arr.append(lf.elem)
            self._parent_tree_nums.append(page_index)
            self._parent_tree_nums.append(Array(arr))

    # -- bookmarks (PDF /Outlines) ------------------------------------------
    def _build_outlines(self) -> None:
        """Generate PDF bookmarks from heading nodes in the manifest."""
        headings: list[dict] = []

        def collect(nodes: list) -> None:
            for node in nodes:
                tag = node.get("tag", "")
                if len(tag) == 2 and tag[0] == "H" and tag[1].isdigit():
                    headings.append(node)
                collect(node.get("children") or [])

        collect(self.manifest.get("nodes", []) or [])
        if not headings:
            return

        outline_root = self.pdf.make_indirect(Dictionary(Type=Name.Outlines))

        # Stack tracks (level, entry) — the open "parent" chain.
        stack: list[tuple[int, Any]] = [(0, outline_root)]
        # Most-recent sibling at each heading level.
        prev_at: dict[int, Any] = {}

        for node in headings:
            level = int(node["tag"][1])
            text = (node.get("text") or node["tag"])[:255]
            page_idx = max(0, min(int(node.get("page", 1)) - 1, len(self.pdf.pages) - 1))
            page_ref = self.pdf.pages[page_idx].obj

            bbox = node.get("bbox")
            y = float(bbox[3]) if bbox else None
            _null = pikepdf.Object.parse(b"null")
            dest = Array([
                page_ref, Name.XYZ,
                _null,
                y if y is not None else _null,
                _null,
            ])

            entry = self.pdf.make_indirect(
                Dictionary(Title=String(text), Dest=dest)
            )

            # Pop stack until parent has a lower level.
            while len(stack) > 1 and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1]
            entry.Parent = parent

            # Sibling linking.
            if level in prev_at:
                prev_at[level].Next = entry
                entry.Prev = prev_at[level]
            else:
                parent.First = entry  # first child of this parent

            parent.Last = entry
            prev_at[level] = entry

            # Clear deeper levels when going back up.
            for k in list(prev_at):
                if k > level:
                    del prev_at[k]

            stack.append((level, entry))

        # Count top-level entries for the root's /Count.
        count = 0
        cur = outline_root.get("/First")
        while cur is not None:
            count += 1
            cur = cur.get("/Next")
        if count:
            outline_root.Count = count

        self.pdf.Root.Outlines = outline_root
        self.report["bookmarks"] = count

    # -- widget tagging (WCAG 4.1.2 / veraPDF 7.18.4) ----------------------
    def _tag_widgets(self) -> None:
        """Wrap each /Widget annotation in a /Form structure element.

        veraPDF clause 7.18.4 requires Widget annotations to be nested inside
        a Form structure element.  We create one Form elem per Widget and
        append it to the Document element, mirroring what _tag_links does for
        hyperlinks.  Each annotation gets a /StructParent key that maps back to
        its Form struct element via the ParentTree.
        """
        document_elem = self.struct_root.get("/K")
        if document_elem is None:
            return
        if isinstance(document_elem, Array):
            document_elem = document_elem[0]

        widgets_tagged = 0
        for page_idx, page in enumerate(self.pdf.pages):
            annots = page.obj.get("/Annots")
            if not annots:
                continue
            for annot_ref in annots:
                try:
                    annot = annot_ref
                    if annot.get("/Subtype") != Name.Widget:
                        continue

                    # Derive an accessible label: /TU (tooltip) on field or parent.
                    label = ""
                    for src in (annot, annot.get("/Parent")):
                        if src is None:
                            continue
                        for key in ("/TU", "/T"):
                            v = src.get(key)
                            if v:
                                label = str(v)
                                break
                        if label:
                            break
                    label = label or "Form field"

                    form_elem = self.pdf.make_indirect(
                        Dictionary(
                            Type=Name.StructElem,
                            S=Name.Form,
                            P=document_elem,
                            T=String(label),
                            Pg=page.obj,
                        )
                    )
                    form_elem.K = self.pdf.make_indirect(
                        Dictionary(Type=Name.OBJR, Obj=annot_ref, Pg=page.obj)
                    )

                    # Allocate a ParentTree key for this annotation.
                    key = self._annot_next_key
                    self._annot_next_key += 1
                    annot_ref.StructParent = key
                    self._parent_tree_nums.append(key)
                    self._parent_tree_nums.append(form_elem)

                    # Append to Document's /K array.
                    kids = document_elem.get("/K")
                    if isinstance(kids, Array):
                        kids.append(form_elem)
                    else:
                        document_elem.K = Array([kids, form_elem] if kids else [form_elem])

                    widgets_tagged += 1
                except Exception:
                    continue

        self.report["widgets_tagged"] = widgets_tagged

    # -- link tagging (WCAG 4.1.2) ------------------------------------------
    def _tag_links(self) -> None:
        """Wrap each /Link annotation in a /Link structure element.

        PDF/UA requires every hyperlink to be a tagged /Link struct element
        with an /Alt or accessible name.  We auto-detect link annotations from
        the existing PDF — no user action needed.
        """
        document_elem = self.struct_root.get("/K")
        if document_elem is None:
            return
        # Unwrap Array([document]) if necessary.
        if isinstance(document_elem, Array):
            document_elem = document_elem[0]

        tagged = 0
        for page_idx, page in enumerate(self.pdf.pages):
            annots = page.obj.get("/Annots")
            if not annots:
                continue
            for annot_ref in annots:
                try:
                    annot = annot_ref
                    if annot.get("/Subtype") != Name.Link:
                        continue

                    # Extract a human-readable name from the action.
                    action = annot.get("/A") or {}
                    uri = ""
                    if action.get("/S") == Name.URI:
                        raw = action.get("/URI")
                        if raw is not None:
                            uri = str(raw)

                    # Generate a descriptive accessible name from the URL so
                    # screen readers announce something meaningful even when
                    # the visible link text is generic ("click here", etc.).
                    try:
                        from .fix_link_text import generate_link_description
                        alt = generate_link_description(uri) if uri else "Link"
                    except Exception:
                        alt = uri[:80] if uri else "Link"

                    # Build /Link struct element parented to Document.
                    link_elem = self.pdf.make_indirect(
                        Dictionary(
                            Type=Name.StructElem,
                            S=Name.Link,
                            P=document_elem,
                            Alt=String(alt),
                            Pg=page.obj,
                        )
                    )

                    # Attach the annotation object reference (/Obj entry).
                    link_elem.K = self.pdf.make_indirect(
                        Dictionary(
                            Type=Name.OBJR,
                            Obj=annot_ref,
                            Pg=page.obj,
                        )
                    )

                    # veraPDF 7.18.1 + 7.18.5: annotation must carry /Contents.
                    annot_ref.Contents = String(alt)

                    # Allocate a unique ParentTree key for this annotation so
                    # StructParent maps correctly (not reusing the page index).
                    key = self._annot_next_key
                    self._annot_next_key += 1
                    annot_ref.StructParent = key
                    self._parent_tree_nums.append(key)
                    self._parent_tree_nums.append(link_elem)

                    # Append to Document's /K array.
                    kids = document_elem.get("/K")
                    if isinstance(kids, Array):
                        kids.append(link_elem)
                    else:
                        document_elem.K = Array([kids, link_elem] if kids else [link_elem])

                    tagged += 1
                except Exception:
                    continue  # skip malformed annotations

        self.report["links_tagged"] = tagged

    # -- top-level orchestration --------------------------------------------
    def apply(self, title: str | None, language: str | None) -> dict:
        self._parent_tree_nums: list = []
        # Annotation ParentTree keys start after page-level entries (0..n-1).
        # We populate this counter before _tag_links / _tag_widgets so both
        # methods can allocate unique keys without collisions.
        self._annot_next_key = len(self.pdf.pages)

        self.build_tree()

        for i, page in enumerate(self.pdf.pages):
            self.remark_page(i, page)
            page.obj.Tabs = Name.S  # tab order follows structure tree (WCAG 2.1.1)

        # Any leaf that never bound to content (no MCID) is reported.
        self.report["unbound_leaves"] = sum(
            1 for lf in self.state.leaves if lf.elem is not None and lf.mcid is None
        )

        # IDTree (name tree) so complex-table /Headers references resolve to
        # their TH elements. Keys must be sorted byte strings.
        if self._id_elems:
            names: list = []
            for key in sorted(self._id_elems):
                names.append(String(key))
                names.append(self._id_elems[key])
            self.struct_root.IDTree = self.pdf.make_indirect(Dictionary(Names=Array(names)))

        # RoleMap for non-standard struct types (PDF/UA-1 §7.1).
        try:
            from .role_map import write_role_map
            write_role_map(self.pdf, self.manifest, self.struct_root)
        except Exception:
            pass

        # Bookmarks from headings (WCAG 2.4.1).
        self._build_outlines()

        # Tag link annotations as /Link struct elements (WCAG 4.1.2 / 7.18.5).
        # Tag widget annotations in /Form struct elements (7.18.4).
        # Both methods append entries to _parent_tree_nums, so ParentTree must
        # be written AFTER they run.
        self._tag_links()
        self._tag_widgets()

        # ParentTree number tree — written last so link/widget entries are included.
        self.struct_root.ParentTree = self.pdf.make_indirect(
            Dictionary(Nums=Array(self._parent_tree_nums))
        )
        self.struct_root.ParentTreeNextKey = self._annot_next_key

        # --- catalog-level PDF/UA requirements ---
        cat = self.pdf.Root
        cat.StructTreeRoot = self.struct_root
        cat.MarkInfo = Dictionary(Marked=True)

        # Auto-populate language and title from manifest when not explicit.
        if not language:
            language = "en-US"
        cat.Lang = String(language)

        # Auto-title from first H1 if manifest didn't supply one.
        if not title:
            title = self._extract_title_from_manifest()

        vp = cat.get("/ViewerPreferences", Dictionary())
        vp.DisplayDocTitle = True
        cat.ViewerPreferences = vp

        self._write_metadata(title)
        return self.report

    def _extract_title_from_manifest(self) -> str:
        """Return text of the first H1 node in the manifest, or empty string."""
        def _find_h1(nodes: list) -> str:
            for node in nodes:
                if node.get("tag") == "H1":
                    return (node.get("text") or "").strip()[:200]
                found = _find_h1(node.get("children") or [])
                if found:
                    return found
            return ""
        return _find_h1(self.manifest.get("nodes", []) or [])

    def _write_metadata(self, title: str | None) -> None:
        title = title or self.manifest.get("document", {}).get("title") \
            or self.manifest.get("document", {}).get("suggestedTitle") or ""
        if title:
            with self.pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                meta["dc:title"] = title
            self.pdf.docinfo[Name.Title] = String(title)

        # Inject the PDF/UA-1 identifier into the XMP packet (pikepdf's metadata
        # API doesn't know the pdfuaid namespace, so patch the stream directly).
        self._inject_pdfua_id(title)

    def _inject_pdfua_id(self, title: str) -> None:
        ns = "http://www.aiim.org/pdfua/ns/id/"
        desc = (
            f'<rdf:Description rdf:about="" xmlns:pdfuaid="{ns}">'
            f"<pdfuaid:part>1</pdfuaid:part>"
            f"</rdf:Description>"
        )
        meta_stream = self.pdf.Root.get("/Metadata")
        if meta_stream is not None:
            try:
                xmp = bytes(meta_stream.read_bytes()).decode("utf-8", "replace")
                if "pdfuaid" not in xmp and "</rdf:RDF>" in xmp:
                    xmp = xmp.replace("</rdf:RDF>", desc + "</rdf:RDF>", 1)
                    meta_stream.write(xmp.encode("utf-8"))
                    return
            except Exception:  # pragma: no cover
                pass

        # No usable XMP yet: build a minimal packet from scratch.
        t = (title or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        xmp = (
            '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title><rdf:Alt><rdf:li xml:lang=\"x-default\">{t}</rdf:li></rdf:Alt></dc:title>"
            "</rdf:Description>"
            f"{desc}"
            "</rdf:RDF></x:xmpmeta>"
            '<?xpacket end="w"?>'
        )
        stream = self.pdf.make_stream(xmp.encode("utf-8"))
        stream.Type = Name.Metadata
        stream.Subtype = Name.XML
        self.pdf.Root.Metadata = stream


def remediate_pdf(pdf_path: str, manifest: dict, out_path: str) -> dict:
    """Write ``manifest`` back into ``pdf_path``, saving a tagged PDF to
    ``out_path``. Returns a summary report dict."""
    doc = manifest.get("document", {}) if isinstance(manifest, dict) else {}
    try:
        with pikepdf.open(pdf_path) as pdf:
            builder = StructTreeBuilder(pdf, manifest)
            report = builder.apply(doc.get("title"), doc.get("language") or "en-US")

            # Wire bidirectional footnote Link annotations (WCAG 2.4.4).
            # Runs after struct tree is built so annotations coexist with tags.
            try:
                from .footnote_links import wire_footnote_links
                fn_pairs = wire_footnote_links(pdf)
                report["footnote_pairs_wired"] = fn_pairs
            except Exception:
                report["footnote_pairs_wired"] = 0

            # PDF/UA-1 catalog compliance (Sprint 14):
            # XMP pdfuaid:part, MarkInfo, ViewerPreferences, /Lang backup.
            try:
                from .pdf_ua_comply import write_pdfua_metadata
                ua_written = write_pdfua_metadata(pdf, manifest)
                report["pdfuaMetadata"] = ua_written
            except Exception:
                report["pdfuaMetadata"] = {}

            # Fix remaining annotation /Contents (Sprint 15).
            try:
                from .annot_check import fix_annotation_contents
                annot_fixed, annot_issues = fix_annotation_contents(pdf)
                report["annotContentsFixed"] = annot_fixed
                report["annotIssues"] = annot_issues
            except Exception:
                report["annotContentsFixed"] = 0
                report["annotIssues"] = []

            # PDF/UA-1 requires PDF 1.4 or later (ISO 14289-1 s6.1).
            # Bump any older version silently before saving.
            try:
                ver = tuple(int(x) for x in pdf.pdf_version.split("."))
                if ver < (1, 4):
                    pdf.pdf_version = "1.4"
                    report["pdfVersionBumped"] = True
            except Exception:
                pass

            pdf.save(out_path)
    except WritebackError:
        raise
    except Exception as exc:
        raise WritebackError(f"Write-back failed: {exc}") from exc
    return report
