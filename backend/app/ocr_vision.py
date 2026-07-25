"""Claude Vision OCR for scanned PDF pages.

When a PDF has no extractable text (a pure image scan), this module:
  1. Renders each scanned page to a PNG at 144 DPI (zoom=2).
  2. Sends the image to Claude Haiku Vision with a structured-extraction prompt.
  3. Returns typed elements (H1–H6 / P / LI / TH / TD / Caption / Figure)
     with bounding boxes converted to PDF user-space coordinates.
  4. Stores the results in the manifest as _ocr_pages so that writeback.py
     can inject an invisible text layer (rendering mode 3) on each page,
     giving the structure tree real content to bind to.

Tesseract (ocr.py) remains as a fallback if the Anthropic API is unavailable.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import uuid

log = logging.getLogger(__name__)

ZOOM: float = 2.0          # 144 DPI — sharp enough for Vision, not too large
MAX_PAGES: int = 30        # safety cap per document
_MIN_TEXT_CHARS: int = 20  # chars per page below which we consider it scanned

_PROMPT = """Analyse this scanned PDF page image.

FIRST decide: is this page primarily a TEXT DOCUMENT or a GRAPHICAL PAGE?

A GRAPHICAL PAGE is a map, diagram, chart, survey plat, engineering drawing,
floor plan, schematic, form with mostly blank fields, or any page where the
dominant content is visual rather than prose text. Scattered labels, lot
numbers, dimension markings, abbreviations, or hand-drawn annotations do NOT
make a page a text document.

A TEXT DOCUMENT has flowing prose sentences, paragraphs, and/or structured
headings. Most elements should be full readable sentences or clear headings.

---

If GRAPHICAL PAGE → return exactly ONE element of type Figure covering the
whole page, with a detailed "alt" field describing what the image shows
(e.g. "Land survey plat showing lots 18-24 in Block 1, Primrose Park,
Portland Oregon, dated June 1954, scale 1:50").

If TEXT DOCUMENT → extract all readable text with semantic structure.

---

Return ONLY a JSON object in this exact shape (no preamble, no markdown):
{
  "elements": [
    {
      "type": "H1",
      "text": "exact text here",
      "bbox": [x0, y0, x1, y1]
    }
  ]
}

For Figure elements also include "alt" and set "text" to "":
    {"type": "Figure", "text": "", "alt": "description here", "bbox": [0, 0, 1, 1]}

bbox: fractional coordinates, origin top-left (0.0–1.0).
  x0,y0 = top-left corner  x1,y1 = bottom-right corner

Text document element types:
  H1  main document/page title
  H2  section heading
  H3  sub-section heading
  H4 H5 H6  lower-level headings
  P   body paragraph (must be a full sentence or meaningful phrase, NOT a label or number)
  LI  list item (include bullet symbol in text if visible)
  TH  table header cell
  TD  table data cell
  Caption  image or table caption

Rules:
  - Preserve exact text including punctuation and numbers
  - If a paragraph spans multiple visual lines, include all lines in one element
  - For tables, emit one TH/TD per cell
  - Ignore page numbers or running headers/footers (they will be auto-artifacted)
  - When in doubt about graphical vs text, choose Figure
"""


# ---------------------------------------------------------------------------
# Graphical-page detection (Python-side safety net)
# ---------------------------------------------------------------------------

def _is_graphical_response(elements: list[dict]) -> bool:
    """Return True if the OCR elements look like a map/diagram rather than a text doc.

    Heuristics:
    - No heading elements (H1-H6) at all, AND
    - Average words per element < 4, AND
    - At least 5 elements (sparse labels, not a single-paragraph page)
    """
    if not elements:
        return False
    HEADING_TAGS = {"H1", "H2", "H3", "H4", "H5", "H6"}
    has_heading = any(el.get("type", "") in HEADING_TAGS for el in elements)
    if has_heading:
        return False  # Real document structure present
    if len(elements) < 5:
        return False  # Too few elements to be a noisy map
    word_counts = [len(el.get("text", "").split()) for el in elements]
    avg_words = sum(word_counts) / len(word_counts)
    short_frac = sum(1 for w in word_counts if w <= 3) / len(word_counts)
    # Graphical if average < 4 words AND 70%+ elements are short labels
    return avg_words < 4.0 and short_frac >= 0.70


def _collapse_to_figure(elements: list[dict], page_w: float, page_h: float) -> list[dict]:
    """Collapse all elements into a single full-page Figure with a generated alt text."""
    # Assemble all text into a description if Vision didn't give us a good alt
    all_text = " ".join(el.get("text", "").strip() for el in elements if el.get("text", "").strip())
    alt = (
        f"Scanned graphical page containing the following text labels: {all_text[:300]}"
        if all_text else "Scanned graphical page (map, diagram, or technical drawing)"
    )
    return [{
        "type": "Figure",
        "text": "",
        "alt": alt,
        "bbox_pdf": [0.0, 0.0, page_w, page_h],
    }]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _client():
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set — Vision OCR unavailable")
    return anthropic.Anthropic(api_key=key)


def _render_page(pdf_path: str, page_index: int) -> tuple[bytes, float, float]:
    """Render one page to PNG. Returns (png_bytes, page_width_pt, page_height_pt)."""
    import fitz
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_index]
        mat = fitz.Matrix(ZOOM, ZOOM)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
        png = pix.tobytes("png")
        w_pt = float(page.rect.width)
        h_pt = float(page.rect.height)
    finally:
        doc.close()
    return png, w_pt, h_pt


def _frac_to_pdf(
    x0f: float, y0f: float, x1f: float, y1f: float,
    page_w: float, page_h: float,
) -> list[float]:
    """Convert fractional bbox (origin top-left) → PDF user-space (origin bottom-left)."""
    # Clamp fractions to valid range
    x0f, y0f, x1f, y1f = (
        max(0.0, min(1.0, x0f)),
        max(0.0, min(1.0, y0f)),
        max(0.0, min(1.0, x1f)),
        max(0.0, min(1.0, y1f)),
    )
    x0 = x0f * page_w
    x1 = x1f * page_w
    # Flip y axis: PDF y increases upward; image y increases downward
    y0 = (1.0 - y1f) * page_h   # bottom of element in PDF coords
    y1 = (1.0 - y0f) * page_h   # top    of element in PDF coords
    return [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)]


# WinAnsiEncoding Unicode → byte for the 0x80-0x9F band (smart quotes, dashes,
# bullet, ellipsis, euro, trademark, …). ASCII (0x20-0x7E) and Latin-1
# (0xA0-0xFF) are identity, so only this band needs an explicit table.
_WINANSI_HIGH: dict[int, int] = {
    0x20AC: 0x80, 0x201A: 0x82, 0x0192: 0x83, 0x201E: 0x84, 0x2026: 0x85,
    0x2020: 0x86, 0x2021: 0x87, 0x02C6: 0x88, 0x2030: 0x89, 0x0160: 0x8A,
    0x2039: 0x8B, 0x0152: 0x8C, 0x017D: 0x8E, 0x2018: 0x91, 0x2019: 0x92,
    0x201C: 0x93, 0x201D: 0x94, 0x2022: 0x95, 0x2013: 0x96, 0x2014: 0x97,
    0x02DC: 0x98, 0x2122: 0x99, 0x0161: 0x9A, 0x203A: 0x9B, 0x0153: 0x9C,
    0x017E: 0x9E, 0x0178: 0x9F,
}


def _to_winansi_byte(cp: int) -> int:
    """Map a Unicode code point to its WinAnsiEncoding byte, or 0x3F ('?')."""
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    return _WINANSI_HIGH.get(cp, 0x3F)


def _encode_pdf_string(text: str) -> bytes:
    """Encode text as a single-byte WinAnsiEncoding PDF string literal.

    The invisible text layer is drawn with the simple WinAnsi /F_OCR font, so
    every byte must be a valid WinAnsi code with a real glyph. A previous
    version fell back to UTF-16BE for any non-Latin-1 character (e.g. a curly
    quote), but those 2-byte sequences were then read by the WinAnsi font as
    single bytes — the 0x00 high bytes resolved to the .notdef glyph, failing
    veraPDF 7.21.8-1 (.notdef reference) and 7.21.7-2 (.notdef → Unicode 0) on
    every such run. WinAnsi covers the common typographic characters (smart
    quotes, en/em dashes, bullet, ellipsis, euro); anything it cannot represent
    degrades to '?' rather than corrupting the whole run.
    """
    encoded = bytes(_to_winansi_byte(ord(ch)) for ch in text)
    escaped = (
        encoded
        .replace(b"\\", b"\\\\")
        .replace(b"(", b"\\(")
        .replace(b")", b"\\)")
        .replace(b"\r", b"\\r")
        .replace(b"\n", b"\\n")
    )
    return b"(" + escaped + b")"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_page_scanned(pdf_path: str, page_index: int) -> bool:
    """Return True if the page has almost no extractable text but has images."""
    try:
        import fitz
        doc = fitz.open(pdf_path)
        page = doc[page_index]
        text_len = len(page.get_text("text").strip())
        has_img = len(page.get_images(full=False)) > 0
        doc.close()
        return text_len < _MIN_TEXT_CHARS and has_img
    except Exception:
        return False


def detect_scanned_pages(pdf_path: str) -> list[int]:
    """Return 0-indexed list of pages that appear to be scanned (image-only)."""
    try:
        import fitz
        doc = fitz.open(pdf_path)
        result = []
        for i in range(len(doc)):
            page = doc[i]
            text_len = len(page.get_text("text").strip())
            has_img = len(page.get_images(full=False)) > 0
            if text_len < _MIN_TEXT_CHARS and has_img:
                result.append(i)
        doc.close()
        return result
    except Exception:
        return []


def ocr_page(pdf_path: str, page_index: int) -> tuple[list[dict], float, float]:
    """
    OCR one page with Claude Vision.

    Returns (elements, page_width_pt, page_height_pt).
    Each element dict: {"type", "text", "alt", "bbox_pdf": [x0,y0,x1,y1]}
    bbox_pdf is in PDF user-space coordinates (points, origin bottom-left).
    """
    try:
        png_bytes, page_w, page_h = _render_page(pdf_path, page_index)
    except Exception as exc:
        log.warning("ocr_vision: render failed page %d: %s", page_index, exc)
        return [], 612.0, 792.0

    b64 = base64.standard_b64encode(png_bytes).decode()

    try:
        resp = _client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": _PROMPT},
                ],
            }],
        )
    except Exception as exc:
        log.warning("ocr_vision: API call failed page %d: %s", page_index, exc)
        return [], page_w, page_h

    raw = (resp.content[0].text if resp.content else "").strip()
    # Strip markdown fences if the model wrapped the JSON
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("ocr_vision: JSON parse failed page %d — raw: %.200r", page_index, raw)
        return [], page_w, page_h

    elements: list[dict] = []
    for el in data.get("elements", []):
        tag = str(el.get("type", "P")).strip()
        text = str(el.get("text", "")).strip()
        alt = str(el.get("alt", "")).strip()
        bbox_frac = el.get("bbox")
        if bbox_frac and len(bbox_frac) == 4:
            try:
                f = [float(v) for v in bbox_frac]
                bbox_pdf = _frac_to_pdf(f[0], f[1], f[2], f[3], page_w, page_h)
            except Exception:
                bbox_pdf = [0.0, 0.0, page_w, 12.0]
        else:
            bbox_pdf = [0.0, 0.0, page_w, 12.0]

        elements.append({"type": tag, "text": text, "alt": alt, "bbox_pdf": bbox_pdf})

    # Safety net: if Vision returned many short label fragments with no headings,
    # the page is a map/diagram — collapse to a single Figure rather than producing
    # a flood of meaningless P tags (false positives on survey plats, charts, etc.)
    if _is_graphical_response(elements):
        log.info(
            "ocr_vision: page %d looks graphical (%d short-label elements) — collapsing to Figure",
            page_index, len(elements),
        )
        elements = _collapse_to_figure(elements, page_w, page_h)

    log.debug("ocr_vision: page %d → %d elements", page_index, len(elements))
    return elements, page_w, page_h


def ocr_document(pdf_path: str, scanned_pages: list[int]) -> dict:
    """
    Run Vision OCR on the given 0-indexed pages.

    Returns a dict keyed by page index (as *string* for JSON compat):
      {
        "0": {"elements": [...], "page_w": 612.0, "page_h": 792.0},
        "3": {...},
      }
    This dict is stored in the manifest as ``_ocr_pages`` and consumed by
    writeback.py to inject the invisible text layer.
    """
    result: dict = {}
    capped = scanned_pages[:MAX_PAGES]
    for i, pg_idx in enumerate(capped):
        log.info("ocr_vision: OCR page %d/%d (index %d)", i + 1, len(capped), pg_idx)
        elements, pw, ph = ocr_page(pdf_path, pg_idx)
        result[str(pg_idx)] = {"elements": elements, "page_w": pw, "page_h": ph}
    return result


def build_manifest_nodes(ocr_doc: dict) -> list[dict]:
    """
    Convert the output of ``ocr_document()`` into manifest-style node dicts
    suitable for inclusion in the autotag manifest.
    """
    nodes: list[dict] = []
    for pg_idx_str in sorted(ocr_doc.keys(), key=int):
        page_num = int(pg_idx_str) + 1  # manifest uses 1-based page numbers
        for el in ocr_doc[pg_idx_str].get("elements", []):
            node: dict = {
                "id": str(uuid.uuid4()),
                "tag": el["type"],
                "text": el["text"],
                "page": page_num,
                "bbox": el["bbox_pdf"],
                "children": [],
            }
            if el["type"] == "Figure":
                node["alt"] = el["alt"]
                node["decorative"] = not bool(el["alt"])
            nodes.append(node)
    return nodes


# ---------------------------------------------------------------------------
# Writeback helper (called from writeback.py)
# ---------------------------------------------------------------------------

def build_ocr_text_stream(elements: list[dict]) -> bytes:
    """
    Return a PDF content stream bytes string that writes each OCR element as
    invisible text (text rendering mode 3 — Tr 3) at its bbox position.

    This stream is appended to the page's /Contents array by writeback.py
    so that remark_page() can find real text operators to bind MCIDs to.
    No BMC/EMC markers are added here — remark_page handles those.
    """
    lines: list[bytes] = []
    for el in elements:
        text = el.get("text", "").strip()
        if not text:
            continue
        bbox = el.get("bbox_pdf") or [0.0, 0.0, 100.0, 12.0]
        x0, y0, x1, y1 = (
            float(bbox[0]), float(bbox[1]),
            float(bbox[2]), float(bbox[3]),
        )
        height = max(y1 - y0, 4.0)
        font_size = max(6.0, min(72.0, height * 0.75))

        lines.append(b"BT")
        lines.append(f"/F_OCR {font_size:.1f} Tf".encode())
        lines.append(b"3 Tr")  # invisible rendering mode
        lines.append(f"{x0:.2f} {y0:.2f} Td".encode())

        # Handle multi-line text (split on newlines, emit Td offsets)
        text_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        first = True
        for line in text_lines:
            if not line.strip():
                continue
            if not first:
                line_h = font_size * 1.2
                lines.append(f"0 {-line_h:.1f} Td".encode())
            lines.append(_encode_pdf_string(line) + b" Tj")
            first = False

        lines.append(b"ET")

    return b"\n".join(lines)
