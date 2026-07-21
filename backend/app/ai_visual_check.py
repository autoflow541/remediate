"""ai_visual_check.py — AI visual review of the human-judgment checkpoints.

veraPDF can verify that alt text *exists*; it cannot verify that the alt text
*matches the image*, that the reading order is *visually sensible*, that
headings *look like* headings, or that artifacted content is *actually*
decorative. Those are the ~47 human-verification Matterhorn checkpoints.

This module renders the remediated PDF's pages and shows them to Claude
(vision) together with the document's structure tree, asking it to review the
judgment areas and flag, per item, whether it looks right or needs human eyes.
The output is an *assistive triage* for the human reviewer — it narrows "check
everything" down to "check these three things on page 4" — and is always
labeled as such. It does not replace human verification and never upgrades the
compliance claim.

Requires ANTHROPIC_API_KEY. Degrades gracefully (available: false) without it.
"""

from __future__ import annotations

import base64
import json
import logging
import os

log = logging.getLogger(__name__)

_MODEL = "claude-opus-4-8"
_MAX_TOKENS = 16000
_MAX_PAGES = 4           # pages rendered into the review (cost/latency cap)
_TARGET_WIDTH = 880      # rendered page width in px — enough for layout judgment

# The judgment areas the reviewer covers, in Matterhorn/WCAG terms.
_CHECKS = ["alt_text", "reading_order", "headings", "decorative", "tables", "title", "other"]

_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "2-3 sentence overall assessment for the human reviewer.",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "check": {"type": "string", "enum": _CHECKS},
                    "verdict": {
                        "type": "string",
                        "enum": ["looks_good", "needs_human", "likely_problem"],
                    },
                    "page": {
                        "type": "integer",
                        "description": "1-indexed page, or 0 for document-level findings.",
                    },
                    "detail": {
                        "type": "string",
                        "description": "What was observed and what the human should confirm.",
                    },
                },
                "required": ["check", "verdict", "page", "detail"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "items"],
    "additionalProperties": False,
}

_INSTRUCTIONS = """You are reviewing a PDF that was just auto-remediated for accessibility \
(PDF/UA structure tags were written into it). The machine checks passed or were reported \
separately; YOUR job is the judgment calls no validator can make. You are given renders of \
the first pages and the document's structure tree (tags in reading order, with alt text).

Review each area and report per-item verdicts:
- alt_text: Does each figure's alt text accurately and usefully describe what is visibly in \
the image? Flag alt that is generic, wrong, or describes the file rather than the content.
- reading_order: Does the tag order match the natural visual reading order (columns, \
sidebars, captions)? Flag places where a screen reader would jump around.
- headings: Do tagged headings correspond to visually prominent section titles, with levels \
matching the visual hierarchy? Flag missed headings and over/under-leveling.
- decorative: Is anything tagged as an artifact/decorative actually informative (or vice \
versa — informative-looking content missing from the structure)?
- tables: Do tables identified in the structure match visual tables, with the plausible \
header row/column marked?
- title: Does the document title in the structure match the visible title on page 1?
- other: Anything else visually wrong for accessibility (e.g., text baked into images, \
color-only meaning, illegible contrast the renders reveal).

Verdicts: "looks_good" (visually consistent — human can spot-check), "needs_human" \
(ambiguous — human must decide), "likely_problem" (visible mismatch — describe it \
precisely, with the page number). Only report pages you were shown. Be specific enough \
that a reviewer can act without re-deriving your reasoning."""


def _render_pages(pdf_path: str, max_pages: int) -> list[tuple[int, bytes]]:
    """Render up to max_pages pages to PNG at ~_TARGET_WIDTH px wide."""
    import fitz

    pages: list[tuple[int, bytes]] = []
    doc = fitz.open(pdf_path)
    try:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            scale = _TARGET_WIDTH / max(page.rect.width, 1)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            pages.append((i + 1, pix.tobytes("png")))
    finally:
        doc.close()
    return pages


def _structure_digest(pdf_path: str) -> dict:
    """Compact structure summary: reading order, figure alts, title/lang."""
    digest: dict = {"elements": [], "title": None, "language": None}

    try:
        from .reading_order import extract_reading_order
        elements = extract_reading_order(pdf_path)
        digest["elements"] = [
            {"type": e.get("type"), "preview": (e.get("preview") or "")[:120]}
            for e in elements[:150]
        ]
    except Exception as exc:
        log.debug("visual_check structure digest: %s", exc)

    try:
        import pikepdf
        with pikepdf.open(pdf_path) as pdf:
            digest["language"] = str(pdf.Root.get("/Lang", "")) or None
            try:
                digest["title"] = str(pdf.docinfo.get("/Title", "")) or None
            except Exception:
                pass
            # Collect Figure alt texts in tree order (first 40).
            alts: list[str] = []

            def walk(node, depth=0):
                if depth > 40 or len(alts) >= 40:
                    return
                try:
                    if str(node.get("/S", "")) == "/Figure":
                        alts.append(str(node.get("/Alt", "")) or "(NO ALT)")
                    k = node.get("/K")
                    kids = list(k) if isinstance(k, pikepdf.Array) else ([k] if k is not None else [])
                    for kid in kids:
                        if hasattr(kid, "get"):
                            walk(kid, depth + 1)
                except Exception:
                    pass

            root = pdf.Root.get("/StructTreeRoot")
            if root is not None:
                walk(root)
            digest["figure_alts"] = alts
    except Exception as exc:
        log.debug("visual_check pikepdf digest: %s", exc)

    return digest


def run_visual_check(pdf_path: str, max_pages: int = _MAX_PAGES) -> dict:
    """Run the AI visual review. Returns a dict safe to JSON-serialize.

    Shape: {available, model, pagesReviewed, totalPages, summary, items[]} or
    {available: false, reason} when the API/key/SDK is missing or errors.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"available": False, "reason": "ANTHROPIC_API_KEY not configured on the engine."}

    try:
        import anthropic
    except ImportError:
        return {"available": False, "reason": "anthropic SDK not installed."}

    try:
        import fitz
        with fitz.open(pdf_path) as d:
            total_pages = d.page_count
    except Exception as exc:
        return {"available": False, "reason": f"Could not open PDF: {exc}"}

    pages = _render_pages(pdf_path, max_pages)
    if not pages:
        return {"available": False, "reason": "No pages could be rendered."}

    digest = _structure_digest(pdf_path)

    content: list[dict] = []
    for page_no, png in pages:
        content.append({"type": "text", "text": f"Page {page_no} render:"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(png).decode(),
            },
        })
    content.append({
        "type": "text",
        "text": (
            "Structure tree of the remediated PDF (tags in reading order, plus figure "
            "alt texts, document title and language):\n"
            + json.dumps(digest, ensure_ascii=False)
        ),
    })

    client = anthropic.Anthropic(api_key=key)
    try:
        response = client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=_INSTRUCTIONS,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:
        log.warning("visual_check: API call failed: %s", exc)
        return {"available": False, "reason": f"AI review failed: {str(exc)[:200]}"}

    if response.stop_reason == "refusal":
        return {"available": False, "reason": "The model declined to review this document."}

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        log.warning("visual_check: unparseable model output")
        return {"available": False, "reason": "AI review returned unparseable output."}

    items = parsed.get("items", [])
    counts = {"looks_good": 0, "needs_human": 0, "likely_problem": 0}
    for it in items:
        v = it.get("verdict")
        if v in counts:
            counts[v] += 1

    return {
        "available": True,
        "model": _MODEL,
        "pagesReviewed": len(pages),
        "totalPages": total_pages,
        "summary": parsed.get("summary", ""),
        "items": items,
        "counts": counts,
        "disclaimer": (
            "AI-assisted review of the judgment checkpoints automated validators cannot "
            "verify. It prioritizes what a human should look at; it does not replace "
            "human verification or change the conformance result."
        ),
    }
