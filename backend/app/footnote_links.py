"""Footnote bidirectional link annotation wiring (WCAG 2.4.4).

Screen readers cannot navigate between a footnote reference mark (superscript
"1" in body text) and its footnote body at the page bottom unless explicit
Link annotations connect them in both directions.

This module:
  1. Scans each page for superscript text spans (font size < 65 % of the
     median body font size) containing numeric or symbolic footnote markers.
  2. Finds matching footnote body text at the bottom 20 % of the same page
     (small font, starts with the same marker).
  3. Creates bidirectional /Link annotations (GoTo action) so AT can jump
     ref → note and note → ref.

Called from remediate_pdf() in writeback.py after the struct tree is written,
so the annotations exist in the final PDF alongside the tagged content.
"""

from __future__ import annotations

import re
import statistics

_MARKER_RE = re.compile(r"^(\d{1,3}|[a-z\*†‡§¶#])[\.\):]?\s*")
_SUPER_SIZE_RATIO = 0.65    # span is "superscript" if size < this × median
_FOOTNOTE_ZONE    = 0.80    # footnote body must start below this fraction of page height
_MIN_BODY_SIZE    = 4.0     # minimum font size to consider (filter artefacts)


def _median_body_size(page) -> float:
    """Estimate the median body font size for a page."""
    sizes = []
    raw = page.get_text("rawdict", flags=0)
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                s = span.get("size", 0)
                if s >= _MIN_BODY_SIZE:
                    sizes.append(s)
    return statistics.median(sizes) if sizes else 12.0


def _collect_superscripts(page, median_size: float) -> list[dict]:
    """Return superscript spans that look like footnote ref marks."""
    results = []
    h = page.rect.height
    threshold = median_size * _SUPER_SIZE_RATIO
    raw = page.get_text("rawdict", flags=0)
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            line_size = statistics.median(
                [sp.get("size", 0) for sp in line.get("spans", []) if sp.get("size", 0) > 0]
                or [0]
            )
            for span in line.get("spans", []):
                size = span.get("size", 0)
                text = (span.get("text") or "").strip()
                if not text or size <= 0:
                    continue
                # Must be significantly smaller than line/page median
                if size >= min(threshold, line_size * 0.8):
                    continue
                m = _MARKER_RE.match(text)
                if not m:
                    continue
                marker = m.group(1)
                x0, y0, x1, y1 = span["bbox"]
                # Convert to PDF coords (bottom-left origin)
                pdf_y0 = h - y1
                pdf_y1 = h - y0
                # Skip if in footnote zone itself (we're looking for ref marks in body)
                if pdf_y0 / h < (1 - _FOOTNOTE_ZONE):
                    # This span is in the body text area (pdf y = 0 at bottom)
                    # Actually: pdf_y0/h < 0.8 means it's in the top 80% (body area)
                    results.append({
                        "marker": marker,
                        "bbox_pdf": [x0, pdf_y0, x1, pdf_y1],
                        "text": text[:20],
                    })
    return results


def _collect_footnote_bodies(page, median_size: float) -> list[dict]:
    """Return text blocks in the bottom zone that start with a footnote marker."""
    results = []
    h = page.rect.height
    raw = page.get_text("rawdict", flags=0)
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        # Block must be in bottom zone (PDF y-coords: bottom = 0, so small y = bottom)
        bx0, by0, bx1, by1 = block["bbox"]
        pdf_by0 = h - by1  # bottom of block in PDF coords
        if pdf_by0 / h > (1 - _FOOTNOTE_ZONE):
            continue  # block is in body area, not footnote zone
        # Extract first line text
        lines = block.get("lines", [])
        if not lines:
            continue
        first_text = "".join(sp.get("text", "") for sp in lines[0].get("spans", [])).strip()
        m = _MARKER_RE.match(first_text)
        if not m:
            continue
        marker = m.group(1)
        results.append({
            "marker": marker,
            "bbox_pdf": [bx0, pdf_by0, bx1, h - by0],
            "text": first_text[:60],
        })
    return results


def _make_link_annot(pdf, page_obj, rect_pdf: list, dest_page_obj, dest_y: float):
    """Create a /Link annotation with a GoTo action (XYZ destination)."""
    import pikepdf
    from pikepdf import Array, Dictionary, Name, String

    h = float(page_obj.MediaBox[3]) if page_obj.get("/MediaBox") else 792.0
    # rect_pdf is [x0, y0_pdf, x1, y1_pdf] — convert to annotation rect [x0, y0, x1, y1]
    # PDF annotation rects use bottom-left origin, same as rect_pdf — OK as-is.
    x0, y0, x1, y1 = rect_pdf
    annot_rect = Array([
        pikepdf.Decimal(round(x0, 2)),
        pikepdf.Decimal(round(y0, 2)),
        pikepdf.Decimal(round(x1, 2)),
        pikepdf.Decimal(round(y1, 2)),
    ])

    null = pikepdf.Object.parse(b"null")
    dest = Array([dest_page_obj, Name.XYZ, null,
                  pikepdf.Decimal(round(dest_y, 2)), null])

    annot = pdf.make_indirect(Dictionary(
        Type    = Name.Annot,
        Subtype = Name.Link,
        Rect    = annot_rect,
        Border  = Array([pikepdf.Decimal(0), pikepdf.Decimal(0), pikepdf.Decimal(0)]),
        A       = Dictionary(S=Name.GoTo, D=dest),
    ))
    return annot


def _append_annot(page_obj, annot):
    """Append a Link annotation to a page's /Annots array."""
    import pikepdf
    from pikepdf import Array

    existing = page_obj.get("/Annots")
    if existing is None:
        page_obj.Annots = Array([annot])
    elif isinstance(existing, Array):
        existing.append(annot)
    else:
        page_obj.Annots = Array([existing, annot])


def wire_footnote_links(pdf) -> int:
    """Add bidirectional footnote Link annotations to all pages.

    Returns the count of ref↔note pairs wired.
    """
    try:
        import fitz
    except ImportError:
        return 0

    total = 0
    try:
        # We need fitz for text analysis and pikepdf for annotation writing.
        # Open a read-only fitz copy of the already-open pikepdf document by
        # saving to bytes first (avoids touching the temp file path).
        import io
        buf = io.BytesIO()
        pdf.save(buf)
        buf.seek(0)
        doc = fitz.open(stream=buf, filetype="pdf")
    except Exception:
        return 0

    try:
        for page_idx, fitz_page in enumerate(doc):
            try:
                median = _median_body_size(fitz_page)
                supers = _collect_superscripts(fitz_page, median)
                bodies = _collect_footnote_bodies(fitz_page, median)
                if not supers or not bodies:
                    continue

                # Match by marker string
                body_map = {b["marker"]: b for b in bodies}
                pdf_page = pdf.pages[page_idx]

                for sup in supers:
                    body = body_map.get(sup["marker"])
                    if not body:
                        continue

                    # ref → note
                    note_y = body["bbox_pdf"][3]  # top of note block in PDF coords
                    annot_fwd = _make_link_annot(
                        pdf, pdf_page.obj,
                        sup["bbox_pdf"],
                        pdf_page.obj,
                        note_y,
                    )
                    _append_annot(pdf_page.obj, annot_fwd)

                    # note → ref (reverse)
                    ref_y = sup["bbox_pdf"][3]  # top of superscript
                    annot_rev = _make_link_annot(
                        pdf, pdf_page.obj,
                        body["bbox_pdf"],
                        pdf_page.obj,
                        ref_y,
                    )
                    _append_annot(pdf_page.obj, annot_rev)
                    total += 1
            except Exception:
                continue
    finally:
        doc.close()

    return total
