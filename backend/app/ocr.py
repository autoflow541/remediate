"""OCR fallback for scanned / image-only PDFs using Tesseract + PyMuPDF.

When OpenDataLoader finds no text (all-image scan), this module renders each
page with PyMuPDF at 300 dpi and runs Tesseract to recover text, building a
minimal manifest of <P> nodes that the studio can tag and remediate.
"""

from __future__ import annotations

import io
import uuid

try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

try:
    import pytesseract
    from PIL import Image
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

_DPI = 300
_SCALE = _DPI / 72.0  # points -> pixels at target DPI
_MIN_CONF = 30        # discard OCR tokens with confidence below this


def is_scanned(pdf_path: str, char_threshold: int = 50) -> bool:
    """Return True if the PDF contains fewer than *char_threshold* real characters.

    Used to decide whether to fall back to OCR rather than OpenDataLoader.
    """
    if not HAS_FITZ:
        return False
    doc = fitz.open(pdf_path)
    try:
        total = sum(len(p.get_text()) for p in doc)
    finally:
        doc.close()
    return total < char_threshold


def ocr_pdf(pdf_path: str) -> dict:
    """OCR a scanned PDF and return a draft manifest dict.

    Nodes are all tagged <P>; the studio can promote headings manually or via
    the auto-fix heading tool once the user sets levels.
    """
    if not (HAS_FITZ and HAS_TESSERACT):
        return _empty_manifest(pdf_path)

    doc = fitz.open(pdf_path)
    nodes: list[dict] = []

    try:
        for page_num, page in enumerate(doc):
            mat = fitz.Matrix(_SCALE, _SCALE)
            pixmap = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
            img = Image.open(io.BytesIO(pixmap.tobytes("png")))

            try:
                tsv = pytesseract.image_to_data(
                    img, output_type=pytesseract.Output.DICT, lang="eng"
                )
            except Exception:
                continue

            # Group words by block → one manifest node per block.
            blocks: dict[int, dict] = {}
            for i, word in enumerate(tsv["text"]):
                word = word.strip()
                if not word:
                    continue
                conf = int(tsv["conf"][i])
                if conf < _MIN_CONF:
                    continue
                bnum = tsv["block_num"][i]
                x, y, w, h = (
                    tsv["left"][i], tsv["top"][i],
                    tsv["width"][i], tsv["height"][i],
                )
                if bnum not in blocks:
                    blocks[bnum] = {
                        "words": [],
                        "x0": x, "y0": y,
                        "x1": x + w, "y1": y + h,
                    }
                b = blocks[bnum]
                b["words"].append(word)
                b["x0"] = min(b["x0"], x)
                b["y0"] = min(b["y0"], y)
                b["x1"] = max(b["x1"], x + w)
                b["y1"] = max(b["y1"], y + h)

            for b in blocks.values():
                text = " ".join(b["words"])
                if not text:
                    continue
                # Convert pixel coords back to PDF user-space points.
                inv = 72.0 / _DPI
                bbox = [
                    b["x0"] * inv, b["y0"] * inv,
                    b["x1"] * inv, b["y1"] * inv,
                ]
                nodes.append({
                    "id": str(uuid.uuid4()),
                    "tag": "P",
                    "text": text,
                    "page": page_num + 1,
                    "bbox": bbox,
                    "children": [],
                })
    finally:
        doc.close()

    return {
        "source": {
            "filename": "",
            "engine": "tesseract-ocr",
            "nodeCount": len(nodes),
            "ocr": True,
        },
        "document": {"title": "", "language": "", "suggestedTitle": ""},
        "nodes": nodes,
    }


def _empty_manifest(pdf_path: str = "") -> dict:
    return {
        "source": {"filename": "", "engine": "none", "nodeCount": 0},
        "document": {"title": "", "language": "", "suggestedTitle": ""},
        "nodes": [],
    }
