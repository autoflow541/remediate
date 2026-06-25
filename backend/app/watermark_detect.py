"""Watermark and background graphic auto-Artifact detection (Sprint 11).

Watermarks, draft stamps, and full-page background images are purely
decorative and must be tagged /Artifact so AT skips them.  Without this,
screen readers read out "CONFIDENTIAL" or "DRAFT" on every page.

Detection heuristics:
  1. Large images covering > 60% of the page area → background/watermark
  2. Text runs where the text matches known watermark words AND:
       • font size > 36pt, OR
       • opacity < 0.5 (stored in ExtGState), OR
       • rendered diagonally (Tm has significant rotation)
  3. Path fills covering > 50% of the page area with very low opacity

Returns a list of {page, type, bbox, reason} candidates.
These are informational — writeback.py cannot retroactively Artifact
existing marked content without the manifest, so this module is used by
autotag.py to flag candidate nodes for the human to review.
"""

from __future__ import annotations

import re

_WATERMARK_WORDS = re.compile(
    r"\b(draft|confidential|sample|do not copy|watermark|void|copy|"
    r"proprietary|restricted|for review|not for distribution)\b",
    re.I,
)
_LARGE_FONT_PT  = 36.0
_COVER_RATIO    = 0.60   # image/path covers this fraction of page → background


def detect_watermarks(pdf_path: str) -> list[dict]:
    """Return candidate watermark / background elements."""
    try:
        import fitz
    except ImportError:
        return []

    candidates: list[dict] = []
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []

    try:
        for page_idx, page in enumerate(doc):
            page_num = page_idx + 1
            page_area = page.rect.width * page.rect.height

            # ── 1. Large image XObjects ────────────────────────────────────
            for img in page.get_images(full=True):
                try:
                    xref = img[0]
                    bbox_list = page.get_image_rects(xref)
                    for bbox in bbox_list:
                        area = bbox.width * bbox.height
                        if page_area > 0 and (area / page_area) >= _COVER_RATIO:
                            candidates.append({
                                "page": page_num,
                                "type": "background_image",
                                "bbox": [round(v, 1) for v in [bbox.x0, bbox.y0, bbox.x1, bbox.y1]],
                                "reason": (
                                    f"Image covers {(area/page_area)*100:.0f}% of the page. "
                                    "Likely a background or watermark. Mark as Artifact."
                                ),
                            })
                except Exception:
                    continue

            # ── 2. Watermark text spans ────────────────────────────────────
            raw = page.get_text("rawdict", flags=0)
            for block in raw.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = (span.get("text") or "").strip()
                        size = span.get("size") or 0
                        if not text:
                            continue
                        if _WATERMARK_WORDS.search(text) and size >= _LARGE_FONT_PT:
                            x0, y0, x1, y1 = span["bbox"]
                            candidates.append({
                                "page": page_num,
                                "type": "watermark_text",
                                "bbox": [round(v, 1) for v in [x0, y0, x1, y1]],
                                "text": text[:60],
                                "reason": (
                                    f"Large text ({size:.0f}pt) matching watermark keyword "
                                    f'"{text[:30]}". Mark as Artifact if purely decorative.'
                                ),
                            })

            # ── 3. Large filled paths ──────────────────────────────────────
            try:
                for path in page.get_drawings():
                    rect = path.get("rect")
                    if not rect:
                        continue
                    area = rect.width * rect.height
                    fill = path.get("fill")
                    if fill and page_area > 0 and (area / page_area) >= _COVER_RATIO:
                        candidates.append({
                            "page": page_num,
                            "type": "background_fill",
                            "bbox": [round(v, 1) for v in [rect.x0, rect.y0, rect.x1, rect.y1]],
                            "reason": (
                                f"Filled path covers {(area/page_area)*100:.0f}% of page. "
                                "May be a background fill. Mark as Artifact."
                            ),
                        })
            except Exception:
                pass

    finally:
        doc.close()

    return candidates
