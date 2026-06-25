"""Font embedding auto-repair — Sprint 23 (PDF/UA 7.21).

Attempts to embed fonts that are referenced but not embedded in the PDF.
Unembedded fonts are a PDF/UA-1 conformance failure and break text rendering
for AT users on systems that don't have the font installed.

Strategy:
  1. Walk each page's /Resources /Font dict for fonts missing /FontFile2
  2. Try to locate a matching TrueType on the system via fc-list or path scan
  3. Subset the font to Latin + common ranges with fontTools
  4. Inject the subset as /FontFile2 on the font's /FontDescriptor

Degrades silently if fontTools is not installed.
"""

from __future__ import annotations

import io
import logging
import os
import subprocess

log = logging.getLogger(__name__)

_FONT_SEARCH_DIRS = [
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
    "C:/Windows/Fonts",
]
_SUBSET_UNICODES = list(range(0x20, 0x17F))   # Basic Latin + Latin-1 Supplement


def _locate_system_font(family: str) -> str | None:
    """Return path to a TTF/OTF file matching *family*, or None."""
    # 1. Try fontconfig (Linux / Docker)
    try:
        result = subprocess.run(
            ["fc-list", f":family={family}", "--format=%{{file}}\\n"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                p = line.strip()
                if p and os.path.isfile(p):
                    return p
    except Exception:
        pass

    # 2. Walk known font directories
    needle = family.lower().replace(" ", "").replace("-", "")
    for d in _FONT_SEARCH_DIRS:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for fname in files:
                if not fname.lower().endswith((".ttf", ".otf")):
                    continue
                norm = fname.lower().replace(" ", "").replace("-", "")
                if needle in norm or norm.startswith(needle[:6]):
                    return os.path.join(root, fname)
    return None


def _subset_font(path: str) -> bytes | None:
    """Return a subsetted TTF/OTF as bytes, or None on failure."""
    try:
        from fontTools.ttLib import TTFont
        from fontTools import subset as ft_subset

        tt = TTFont(path)
        options = ft_subset.Options()
        options.layout_features = []
        options.name_IDs = [1, 2, 4]
        subsetter = ft_subset.Subsetter(options=options)
        subsetter.populate(unicodes=_SUBSET_UNICODES)
        subsetter.subset(tt)
        buf = io.BytesIO()
        tt.save(buf)
        return buf.getvalue()
    except Exception as exc:
        log.debug("font_embed subset: %s", exc)
        return None


def embed_fonts(pdf_path: str, font_issues: list[dict]) -> tuple[int, list[str]]:
    """Embed missing fonts identified by font_check.py.

    Returns (fonts_embedded_count, notes).  PDF is modified in-place.
    """
    unembedded = [f for f in font_issues if f.get("issue") == "not_embedded"]
    if not unembedded:
        return 0, []

    try:
        import pikepdf
        from fontTools.ttLib import TTFont  # noqa: F401  — availability check
    except ImportError:
        log.debug("font_embed: fontTools not available")
        return 0, []

    embedded = 0
    notes: list[str] = []
    seen: set[str] = set()  # avoid double-embedding same font

    try:
        pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)

        for page in pdf.pages:
            res = page.get("/Resources")
            if not res:
                continue
            fonts = res.get("/Font")
            if not fonts:
                continue
            for _, font_ref in fonts.items():
                try:
                    obj = font_ref.get_object() if hasattr(font_ref, "get_object") else font_ref
                    base_font = str(obj.get("/BaseFont", "")).lstrip("/")
                    if not base_font or base_font in seen:
                        continue
                    # Skip if already embedded
                    desc_ref = obj.get("/FontDescriptor")
                    if not desc_ref:
                        continue
                    desc = desc_ref.get_object() if hasattr(desc_ref, "get_object") else desc_ref
                    if desc.get("/FontFile") or desc.get("/FontFile2") or desc.get("/FontFile3"):
                        continue

                    # Check if this font is in the unembedded list
                    matched = any(
                        base_font.lower() in (f.get("font_name") or "").lower()
                        for f in unembedded
                    )
                    if not matched:
                        continue

                    # Try to find and embed
                    sys_path = _locate_system_font(base_font)
                    if not sys_path:
                        log.debug("font_embed: no system font for %r", base_font)
                        continue

                    font_data = _subset_font(sys_path)
                    if not font_data:
                        continue

                    stream = pikepdf.Stream(pdf, font_data)
                    desc[pikepdf.Name("/FontFile2")] = stream
                    seen.add(base_font)
                    embedded += 1
                    notes.append(
                        f"Embedded {base_font} from {os.path.basename(sys_path)}"
                    )
                    log.info("font_embed: embedded %r", base_font)
                except Exception as exc:
                    log.debug("font_embed: %s", exc)

        if embedded > 0:
            pdf.save(pdf_path)

        pdf.close()
    except Exception as exc:
        log.warning("font_embed: failed: %s", exc)

    return embedded, notes
