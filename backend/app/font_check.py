"""Font embedding and ToUnicode CMap checker (Sprint 8).

Two font-level requirements for accessible PDFs:

1. **Font embedding** — all fonts must be embedded so AT and renderers can
   access glyph outlines.  A non-embedded font falls back to a system
   substitute which may differ in glyph shapes and metrics.

2. **ToUnicode CMap** — every font must include a /ToUnicode CMap so glyphs
   can be mapped to Unicode code points.  Without it, copy-paste produces
   garbage and screen readers cannot read the text.

Both are required by PDF/UA-1 and caught by veraPDF, but reported here with
richer per-font detail so remediators know which fonts to fix.
"""

from __future__ import annotations


def check_fonts(pdf_path: str) -> list[dict]:
    """Return a list of font issues (missing embed / missing ToUnicode).

    Each dict: {font_name, page, issue, description}
    Returns [] if pikepdf is unavailable or the PDF has no font problems.
    """
    try:
        import pikepdf
    except ImportError:
        return []

    issues: list[dict] = []
    seen: set[str] = set()  # (font_name, issue) pairs — deduplicate across pages

    try:
        with pikepdf.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                page_num = page_idx + 1
                try:
                    resources = page.obj.get("/Resources")
                    if not resources:
                        continue
                    fonts = resources.get("/Font")
                    if not fonts:
                        continue
                    for key in fonts.keys():
                        try:
                            font_obj = fonts[key]
                            name = str(font_obj.get("/BaseFont") or key).lstrip("/")
                            subtype = str(font_obj.get("/Subtype") or "").lstrip("/")

                            # ── Embedding check ──────────────────────────────
                            descriptor = font_obj.get("/FontDescriptor")
                            is_embedded = False
                            if descriptor:
                                for embed_key in ("/FontFile", "/FontFile2", "/FontFile3"):
                                    if descriptor.get(embed_key):
                                        is_embedded = True
                                        break
                            # Type0 (CIDFont wrapper) — check descendant
                            if subtype == "Type0" and not is_embedded:
                                descendants = font_obj.get("/DescendantFonts")
                                if descendants:
                                    try:
                                        desc_font = descendants[0]
                                        dd = desc_font.get("/FontDescriptor")
                                        if dd:
                                            for ek in ("/FontFile", "/FontFile2", "/FontFile3"):
                                                if dd.get(ek):
                                                    is_embedded = True
                                                    break
                                    except Exception:
                                        pass

                            key_embed = (name, "not_embedded")
                            if not is_embedded and key_embed not in seen:
                                seen.add(key_embed)
                                issues.append({
                                    "font_name": name,
                                    "page": page_num,
                                    "issue": "not_embedded",
                                    "description": (
                                        f"Font \"{name}\" is not embedded. AT and renderers "
                                        "will substitute a system font, breaking glyph fidelity "
                                        "and potentially making text unreadable. Embed the font "
                                        "in the source document before exporting to PDF."
                                    ),
                                })

                            # ── ToUnicode check ──────────────────────────────
                            has_to_unicode = bool(font_obj.get("/ToUnicode"))
                            # Type0: ToUnicode may be on the font or its descendant
                            if not has_to_unicode and subtype == "Type0":
                                descendants = font_obj.get("/DescendantFonts")
                                if descendants:
                                    try:
                                        if descendants[0].get("/ToUnicode"):
                                            has_to_unicode = True
                                    except Exception:
                                        pass

                            key_uni = (name, "no_to_unicode")
                            if not has_to_unicode and key_uni not in seen:
                                seen.add(key_uni)
                                issues.append({
                                    "font_name": name,
                                    "page": page_num,
                                    "issue": "no_to_unicode",
                                    "description": (
                                        f"Font \"{name}\" has no /ToUnicode CMap. "
                                        "Screen readers and copy-paste will produce garbled "
                                        "text for glyphs in this font. Add a ToUnicode CMap "
                                        "or re-export from the source with Unicode mapping enabled."
                                    ),
                                })
                        except Exception:
                            continue
                except Exception:
                    continue
    except Exception:
        return []

    return issues
