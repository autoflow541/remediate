"""
Auto-describe Figure nodes using Claude vision (claude-haiku-4-5-20251001).
Called after autotag to pre-fill alt text for images.
Images that can't be described are added to manifest._questions for human input.
Falls back silently on API errors.
"""

import base64
import os

import fitz  # PyMuPDF


def _render_region(doc: fitz.Document, page_num: int, bbox: list) -> bytes | None:
    if page_num < 0 or page_num >= len(doc):
        return None
    page = doc[page_num]
    l, b, r, t = bbox
    h = page.rect.height
    rect = fitz.Rect(l, h - t, r, h - b)
    if rect.is_empty or rect.width < 4 or rect.height < 4:
        return None
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, colorspace=fitz.csRGB)
    return pix.tobytes("png")


def _render_thumbnail(doc: fitz.Document, page_num: int, bbox: list, max_px: int = 320) -> str | None:
    """Render a region and return a data-URI for use in the hallway UI."""
    raw = _render_region(doc, page_num, bbox)
    if not raw:
        return None
    b64 = base64.standard_b64encode(raw).decode()
    return f"data:image/png;base64,{b64}"


def _describe_image(client, img_bytes: bytes) -> str:
    img_b64 = base64.standard_b64encode(img_bytes).decode()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": img_b64},
                },
                {
                    "type": "text",
                    "text": (
                        "Classify this image from a PDF document. Respond with EXACTLY one of:\n"
                        "1. decorative — if it is purely decorative (border, divider, background "
                        "pattern, watermark, or logo with no informational content)\n"
                        "2. image_of_text — if the image is primarily or entirely made up of "
                        "text (a screenshot of a webpage/app, a photo of a document, a scanned "
                        "page, a code listing rendered as an image, or any image where the main "
                        "content is readable text)\n"
                        "3. Otherwise write a concise alt text description (1-2 sentences) "
                        "that describes what the image communicates visually."
                    ),
                },
            ],
        }],
    )
    return response.content[0].text.strip()


def _ocr_image(img_bytes: bytes) -> str | None:
    """Run Tesseract OCR on an image and return extracted text, or None."""
    try:
        import pytesseract
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(img_bytes))
        clean = " ".join(pytesseract.image_to_string(img, config="--psm 6").split())
        return clean if len(clean) > 5 else None
    except Exception:
        return None


def _walk_and_describe(nodes: list, doc: fitz.Document, client, questions: list) -> list:
    result = []
    for node in nodes:
        if node.get("tag") == "Figure" and not node.get("alt") and not node.get("decorative"):
            bbox = node.get("bbox")
            page = node.get("page", 1) - 1  # 0-indexed
            if bbox and len(bbox) == 4:
                try:
                    img_bytes = _render_region(doc, page, bbox)
                    if img_bytes:
                        description = _describe_image(client, img_bytes)
                        desc_lower = description.lower().strip()
                        if desc_lower == "decorative":
                            node = {**node, "decorative": True}
                        elif desc_lower == "image_of_text":
                            # Flag as image-of-text (WCAG 1.4.5) for the audit report.
                            # Try Tesseract OCR to pre-fill the alt text — the user
                            # just confirms or corrects instead of typing from scratch.
                            node = {**node, "imageOfText": True}
                            thumbnail = None
                            try:
                                thumbnail = _render_thumbnail(doc, page, bbox)
                            except Exception:
                                pass
                            ocr_text = _ocr_image(img_bytes) if img_bytes else None
                            if ocr_text:
                                hint = (
                                    f"OCR extracted: \"{ocr_text[:200]}\"\n"
                                    "Confirm this text or correct it. "
                                    "Mark as decorative if it duplicates adjacent live text."
                                )
                            else:
                                hint = (
                                    "This image appears to contain text. "
                                    "Provide the verbatim text, or mark it as decorative if it "
                                    "duplicates adjacent live text."
                                )
                            questions.append({
                                "type": "image_alt",
                                "nodeId": node["id"],
                                "page": page + 1,
                                "imageData": thumbnail,
                                "hint": hint,
                                "ocrPreFill": ocr_text or "",
                            })
                        else:
                            node = {**node, "alt": description}
                    else:
                        # Region too small or unrenderable — ask the user
                        thumbnail = _render_thumbnail(doc, page, bbox)
                        questions.append({
                            "type": "image_alt",
                            "nodeId": node["id"],
                            "page": page + 1,
                            "imageData": thumbnail,
                        })
                except Exception:
                    # API error — ask the user rather than silently drop
                    thumbnail = None
                    try:
                        thumbnail = _render_thumbnail(doc, page, bbox)
                    except Exception:
                        pass
                    questions.append({
                        "type": "image_alt",
                        "nodeId": node["id"],
                        "page": page + 1,
                        "imageData": thumbnail,
                    })

        if node.get("children"):
            node = {**node, "children": _walk_and_describe(node["children"], doc, client, questions)}

        result.append(node)
    return result


def describe_figures(pdf_path: str, manifest: dict) -> dict:
    """
    Enrich manifest Figure nodes with AI-generated alt text.
    Nodes that can't be described are added to manifest._questions.
    Returns manifest unchanged if ANTHROPIC_API_KEY is not set.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return manifest

    nodes = manifest.get("nodes", [])

    # Quick check — any untagged Figure nodes at all?
    has_figure = False
    def _check(ns):
        nonlocal has_figure
        for n in ns:
            if n.get("tag") == "Figure" and not n.get("alt") and not n.get("decorative"):
                has_figure = True
                return
            _check(n.get("children") or [])
    _check(nodes)
    if not has_figure:
        return manifest

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        doc = fitz.open(pdf_path)
        questions: list = []
        try:
            enriched = _walk_and_describe(nodes, doc, client, questions)
        finally:
            doc.close()

        result = {**manifest, "nodes": enriched}
        if questions:
            existing = list(result.get("_questions") or [])
            result["_questions"] = existing + questions
        return result
    except Exception:
        return manifest
