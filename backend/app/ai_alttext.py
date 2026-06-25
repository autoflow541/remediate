"""AI alt text generation for figures — Sprint 20 (WCAG 1.1.1).

When a Figure node has no alt text or a low quality score (≤ 2),
extracts the image region from the PDF using PyMuPDF and asks
Claude Haiku (vision) to generate a concise, accurate alt text.

Requires ANTHROPIC_API_KEY environment variable.
Degrades silently if the key is absent or the SDK is unavailable.
"""

from __future__ import annotations

import base64
import logging
import os

log = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 100
_SCORE_THRESHOLD = 2   # auto-generate when quality score ≤ this
_MIN_BBOX_AREA = 400   # skip tiny figures (< 20×20 pts)


def _extract_image_region(pdf_path: str, page_num: int, bbox: list) -> bytes | None:
    """Render the figure bounding box as a PNG (2× zoom)."""
    try:
        import fitz
        doc = fitz.open(pdf_path)
        try:
            page = doc[page_num - 1]
            h = page.rect.height
            # Convert from PDF space (y-up) → MuPDF space (y-down)
            x0, y0, x1, y1 = bbox[0], bbox[1], bbox[2], bbox[3]
            mupdf_rect = fitz.Rect(x0, h - y1, x1, h - y0)
            clip = mupdf_rect & page.rect
            if clip.is_empty or clip.width < 4 or clip.height < 4:
                return None
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip)
            return pix.tobytes("png")
        finally:
            doc.close()
    except Exception as exc:
        log.debug("ai_alttext extract: %s", exc)
        return None


def _call_claude(client, image_png: bytes, existing_alt: str) -> str:
    context = f" Existing (poor) description to improve: '{existing_alt}'." if existing_alt else ""
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.standard_b64encode(image_png).decode(),
                    },
                },
                {
                    "type": "text",
                    "text": (
                        f"Write a concise alt text (≤15 words) for this figure in an "
                        f"accessibility context.{context} Describe what is shown factually. "
                        "Do NOT start with 'Image of', 'Figure', or 'This'. "
                        "Output only the alt text."
                    ),
                },
            ],
        }],
    )
    return resp.content[0].text.strip()


def generate_alt_texts(pdf_path: str, manifest: dict) -> tuple[dict, int]:
    """Generate AI alt text for figures missing or low-quality descriptions.

    Returns (updated_manifest, generated_count).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return manifest, 0

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        return manifest, 0

    generated = 0

    def _process_node(node: dict) -> None:
        nonlocal generated
        if node.get("tag") != "Figure":
            return
        existing = (node.get("alt") or "").strip()
        score = node.get("alt_quality_score", 5)
        needs = (
            not existing
            or existing.lower() in ("", "figure", "image", "photo")
            or score <= _SCORE_THRESHOLD
        )
        if not needs:
            return
        bbox = node.get("bbox")
        page = node.get("page", 1)
        if not bbox or len(bbox) < 4:
            return
        w = abs(bbox[2] - bbox[0])
        h = abs(bbox[3] - bbox[1])
        if w * h < _MIN_BBOX_AREA:
            return
        img = _extract_image_region(pdf_path, page, bbox)
        if not img:
            return
        try:
            new_alt = _call_claude(client, img, existing)
            if new_alt:
                node["alt"] = new_alt
                node["alt_ai_generated"] = True
                generated += 1
                log.info("ai_alttext: page %d → %r", page, new_alt[:60])
        except Exception as exc:
            log.debug("ai_alttext generate: page %d: %s", page, exc)

    for node in manifest.get("nodes", []):
        _process_node(node)
        for child in node.get("children", []):
            _process_node(child)

    manifest.setdefault("source", {})["aiAltGenerated"] = generated
    return manifest, generated
