"""alt_fix.py — Vision AI fills in empty / missing Figure /Alt text.

Walks the structure tree looking for Figure elements whose /Alt is absent,
empty, or a known placeholder.  For each, renders the bounding region of the
page at 144 DPI (2× for clarity), encodes as JPEG, and calls Claude Vision
(claude-haiku-4-5-20251001) to generate a concise, descriptive alt text.

Requires:
  - pikepdf       (struct tree access + /Alt write-back)
  - fitz/PyMuPDF  (page rendering)
  - anthropic     (Vision API)

All changes are in-place.  Returns (fixes_applied, notes).
"""

from __future__ import annotations

import base64
import io
import logging
import os

log = logging.getLogger(__name__)

_PLACEHOLDER: set[str] = {
    "", " ", "image", "figure", "img", "photo", "picture", "graphic",
    "illustration", "chart", "graph", "diagram",
}

_SYSTEM = (
    "You write concise, accurate alt text for PDF figures. "
    "Describe what the image shows in one to three sentences. "
    "Do not start with 'Image of' or 'Picture of'. "
    "Be specific — include numbers, labels, and key visual details. "
    "If it is a decorative element with no informational content, reply with exactly: DECORATIVE"
)


def _render_region(page, bbox: list[float] | None, dpi: int = 144) -> bytes | None:
    """Render the bbox region of a PyMuPDF page to JPEG bytes."""
    try:
        import fitz
        scale = dpi / 72.0
        if bbox and len(bbox) == 4:
            rect = fitz.Rect(bbox[0], bbox[1], bbox[2], bbox[3])
            # Clip to page bounds
            rect = rect & page.rect
            if rect.is_empty or rect.get_area() < 100:
                rect = page.rect   # fallback to full page
        else:
            rect = page.rect
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, clip=rect, colorspace=fitz.csRGB, alpha=False)
        return pix.tobytes("jpeg")
    except Exception as exc:
        log.debug("alt_fix: render error: %s", exc)
        return None


def _call_vision(image_bytes: bytes) -> str | None:
    """Call Claude Vision to generate alt text for an image."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        b64 = base64.standard_b64encode(image_bytes).decode()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=_SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": "Write alt text for this image."},
                ],
            }],
        )
        return msg.content[0].text.strip() if msg.content else None
    except Exception as exc:
        log.debug("alt_fix: vision call error: %s", exc)
        return None


def fix_alt_text(pdf_path: str) -> tuple[int, list[str]]:
    """Fill in missing/empty /Alt on Figure struct elements via Vision AI.

    Returns (fixes_applied, notes).  PDF modified in-place.
    """
    try:
        import pikepdf
        import fitz
    except ImportError as exc:
        log.debug("alt_fix: missing dependency %s — skipped", exc)
        return 0, []

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.debug("alt_fix: ANTHROPIC_API_KEY not set — skipped")
        return 0, []

    fixes = 0
    notes: list[str] = []

    try:
        doc_fitz = fitz.open(pdf_path)
        pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)

        # Build page index lookup
        page_obj_to_idx: dict = {}
        for idx, page in enumerate(pdf.pages):
            try:
                page_obj_to_idx[page.objgen] = idx
            except Exception:
                pass

        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is None:
            pdf.close()
            doc_fitz.close()
            return 0, []

        def _walk(elem):
            nonlocal fixes
            try:
                tag = str(elem.get("/S", ""))
                if tag == "/Figure":
                    existing_alt = str(elem.get("/Alt", "")).strip()
                    if existing_alt.lower() not in _PLACEHOLDER and len(existing_alt) > 10:
                        pass  # already has good alt text
                    else:
                        # Get page and bbox
                        pg_ref = elem.get("/Pg")
                        pg_idx = page_obj_to_idx.get(pg_ref.objgen if pg_ref else None, 0)
                        # Try to get bbox from /BBox attribute
                        bbox = None
                        try:
                            attr = elem.get("/A")
                            if attr:
                                bb = attr.get("/BBox")
                                if bb:
                                    bbox = [float(v) for v in bb]
                        except Exception:
                            pass

                        page = doc_fitz[pg_idx]
                        image_bytes = _render_region(page, bbox)
                        if image_bytes:
                            alt = _call_vision(image_bytes)
                            if alt and alt != "DECORATIVE":
                                elem["/Alt"] = pikepdf.String(alt)
                                fixes += 1
                                short = alt[:60] + "…" if len(alt) > 60 else alt
                                notes.append(f"Page {pg_idx + 1}: Figure /Alt set: {short!r}")
                            elif alt == "DECORATIVE":
                                # Mark as artifact by removing from struct (complex) — skip for now
                                notes.append(f"Page {pg_idx + 1}: Figure identified as decorative — manual review advised")

                # Recurse
                k = elem.get("/K")
                if k is not None:
                    kids = list(k) if hasattr(k, "__iter__") and \
                        not isinstance(k, (str, bytes)) else [k]
                    for kid in kids:
                        if hasattr(kid, "get"):
                            _walk(kid)
            except Exception:
                pass

        try:
            top_kids = struct_root.get("/K")
            if top_kids is not None:
                items = list(top_kids) if hasattr(top_kids, "__iter__") and \
                    not isinstance(top_kids, (str, bytes)) else [top_kids]
                for item in items:
                    if hasattr(item, "get"):
                        _walk(item)
        except Exception as exc:
            log.debug("alt_fix: struct walk error: %s", exc)

        if fixes > 0:
            pdf.save(pdf_path)
            log.info("alt_fix: %d Figure /Alt tags generated", fixes)

        pdf.close()
        doc_fitz.close()

    except Exception as exc:
        log.warning("alt_fix: %s", exc)

    return fixes, notes


# Placeholder used when no meaningful alt could be generated. Deliberately
# obvious so alt_quality.py and the human checklist both flag it for review.
_ALT_PLACEHOLDER = "Image — description pending human review"


def ensure_alt_present(pdf_path: str) -> tuple[int, list[str]]:
    """Guarantee every non-artifact Figure has a non-empty /Alt (PDF/UA 7.3-1).

    Runs AFTER the AI pass. Figures the AI described are left alone; any that
    still lack /Alt (AI unavailable, declined, or a region it couldn't caption)
    get an obvious placeholder so the file is machine-conformant, while the
    alt-quality checker and Matterhorn checklist surface them as needing a real
    description. Never overwrites an existing alt.
    """
    try:
        import pikepdf
    except ImportError:
        return 0, []

    filled = 0
    placeholders = {_ALT_PLACEHOLDER.lower(), "image", "figure", "photo", "graphic", ""}
    try:
        with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
            root = pdf.Root.get("/StructTreeRoot")
            if root is None:
                return 0, []
            seen: set = set()
            stack = [root]
            while stack:
                o = stack.pop()
                if not hasattr(o, "get"):
                    continue
                try:
                    og = o.objgen
                    if og != (0, 0):
                        if og in seen:
                            continue
                        seen.add(og)
                except Exception:
                    pass
                try:
                    if str(o.get("/S", "")) == "/Figure":
                        alt = str(o.get("/Alt", "")).strip()
                        if alt.lower() in placeholders:
                            o[pikepdf.Name("/Alt")] = pikepdf.String(_ALT_PLACEHOLDER)
                            filled += 1
                except Exception:
                    pass
                k = o.get("/K")
                if k is not None:
                    stack.extend(list(k) if isinstance(k, pikepdf.Array) else [k])
            if filled:
                pdf.save()
    except Exception as exc:
        log.warning("ensure_alt_present: %s", exc)
        return 0, []

    notes = ([f"{filled} figure(s) given a placeholder alt text pending human "
              "review (PDF/UA 7.3-1)"] if filled else [])
    return filled, notes
