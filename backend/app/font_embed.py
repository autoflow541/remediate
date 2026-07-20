"""Font embedding auto-repair — Sprint 23 (PDF/UA 7.21).

Attempts to embed fonts that are referenced but not embedded in the PDF.
Unembedded fonts are a PDF/UA-1 conformance failure and break text rendering
for AT users on systems that don't have the font installed.

Strategy:
  1. Walk each page's /Resources /Font dict for fonts missing /FontFile2
  2. Try to locate a matching TrueType on the system via fc-list or path scan
  3. If exact match fails, try metric-compatible substitutes (Liberation/DejaVu)
     for standard PDF Type 1 core fonts (Helvetica, Times, Courier, etc.)
  4. Subset the font to Latin + common ranges with fontTools
  5. Inject the subset as /FontFile2 on the font's /FontDescriptor
  6. Inject a /ToUnicode CMap so text extraction and AT work correctly

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

# ── Type 1 core font → metric-compatible TTF substitutes ───────────────────
# Key: normalised base font name (lowercase, no hyphens/spaces/commas)
# Value: fc-list family names to try in order (first found wins)
_TYPE1_SUBSTITUTES: dict[str, list[str]] = {
    "helvetica":            ["Liberation Sans", "LiberationSans", "Arial", "DejaVu Sans", "FreeSans"],
    "helveticabold":        ["Liberation Sans Bold", "LiberationSans-Bold", "Arial Bold", "DejaVu Sans Bold"],
    "helveticaoblique":     ["Liberation Sans Italic", "LiberationSans-Italic", "Arial Italic"],
    "helveticaboldoblique": ["Liberation Sans Bold Italic", "LiberationSans-BoldItalic"],
    "timesroman":           ["Liberation Serif", "LiberationSerif", "Times New Roman", "DejaVu Serif", "FreeSerif"],
    "timesbold":            ["Liberation Serif Bold", "LiberationSerif-Bold", "Times New Roman Bold"],
    "timesitalic":          ["Liberation Serif Italic", "LiberationSerif-Italic"],
    "timesbolditalic":      ["Liberation Serif Bold Italic", "LiberationSerif-BoldItalic"],
    "courier":              ["Liberation Mono", "LiberationMono", "Courier New", "DejaVu Sans Mono", "FreeMono"],
    "courierbold":          ["Liberation Mono Bold", "LiberationMono-Bold", "Courier New Bold"],
    "courieroblique":       ["Liberation Mono Oblique", "LiberationMono-Oblique"],
    "courierboldoblique":   ["Liberation Mono Bold Oblique", "LiberationMono-BoldOblique"],
    "symbol":               ["DejaVu Sans", "FreeSans", "Liberation Sans"],
    "zapfdingbats":         ["DejaVu Sans", "FreeSans", "Liberation Sans"],
    # Common variant spellings
    "arialmt":              ["Arial", "Liberation Sans", "DejaVu Sans"],
    "arialboldmt":          ["Arial Bold", "Liberation Sans Bold"],
    "arial":                ["Arial", "Liberation Sans", "DejaVu Sans"],
    "arialbold":            ["Arial Bold", "Liberation Sans Bold"],
    "arialitalicmt":        ["Arial Italic", "Liberation Sans Italic", "DejaVu Sans Oblique"],
    "arialbolditalicmt":    ["Arial Bold Italic", "Liberation Sans Bold Italic", "DejaVu Sans Bold Oblique"],
    "arialitalic":          ["Arial Italic", "Liberation Sans Italic"],
    "arialbolditalic":      ["Arial Bold Italic", "Liberation Sans Bold Italic"],
    # Windows web fonts → Liberation/DejaVu equivalents
    "verdana":              ["Liberation Sans", "DejaVu Sans", "FreeSans"],
    "verdanabold":          ["Liberation Sans Bold", "DejaVu Sans Bold"],
    "verdanaitalic":        ["Liberation Sans Italic", "DejaVu Sans Oblique"],
    "verdanabolditalic":    ["Liberation Sans Bold Italic", "DejaVu Sans Bold Oblique"],
    "georgia":              ["Liberation Serif", "DejaVu Serif", "FreeSerif"],
    "georgiabold":          ["Liberation Serif Bold", "DejaVu Serif Bold"],
    "georgiaitalic":        ["Liberation Serif Italic", "DejaVu Serif Italic"],
    "georgiabolditalic":    ["Liberation Serif Bold Italic", "DejaVu Serif Bold Italic"],
    "trebuchetms":          ["Liberation Sans", "DejaVu Sans", "FreeSans"],
    "trebuchetmsbold":      ["Liberation Sans Bold", "DejaVu Sans Bold"],
    "trebuchetmsitalic":    ["Liberation Sans Italic", "DejaVu Sans Oblique"],
    "trebuchetmsbolditalic":["Liberation Sans Bold Italic"],
    "timesnewromanpsmt":    ["Liberation Serif", "DejaVu Serif", "FreeSerif"],
    "timesnewromanpsboldmt":["Liberation Serif Bold", "DejaVu Serif Bold"],
    "timesnewromanpsitalicmt":["Liberation Serif Italic", "DejaVu Serif Italic"],
    "timesnewromanpsbolditalicmt":["Liberation Serif Bold Italic"],
    "timesnewroman":        ["Liberation Serif", "DejaVu Serif", "FreeSerif"],
    "timesnewromanbold":    ["Liberation Serif Bold", "DejaVu Serif Bold"],
    "calibri":              ["Liberation Sans", "DejaVu Sans"],
    "calibribold":          ["Liberation Sans Bold", "DejaVu Sans Bold"],
    "cambria":              ["Liberation Serif", "DejaVu Serif"],
    "cambriabold":          ["Liberation Serif Bold", "DejaVu Serif Bold"],
    "tahoma":               ["Liberation Sans", "DejaVu Sans"],
    "tahomabold":           ["Liberation Sans Bold", "DejaVu Sans Bold"],
    "wingdings":            ["DejaVu Sans", "FreeSans"],
    "wingdingsregular":     ["DejaVu Sans", "FreeSans"],
}


def _normalise_name(name: str) -> str:
    """Lowercase, strip hyphens, spaces, commas, underscores, subset prefixes."""
    n = name.lower()
    # Strip subset prefix (e.g. "ABCDEF+Helvetica" → "helvetica")
    if "+" in n:
        n = n.split("+", 1)[1]
    return n.replace("-", "").replace(" ", "").replace(",", "").replace("_", "")


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


def _locate_font_with_fallback(base_font: str) -> str | None:
    """Try exact font name, then Type 1 metric-compatible substitutes."""
    # Exact match first
    p = _locate_system_font(base_font)
    if p:
        return p

    # Substitute lookup
    key = _normalise_name(base_font)
    for sub_family in _TYPE1_SUBSTITUTES.get(key, []):
        p = _locate_system_font(sub_family)
        if p:
            log.info("font_embed: substituting %r with %r (%s)", base_font, sub_family, p)
            return p

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
        log.debug("font_embed subset (fontTools): %s", exc)
        # Fallback: embed raw font bytes without subsetting
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except Exception:
            return None


# WinAnsiEncoding byte → Unicode code point (used to build ToUnicode CMap)
_WIN_ANSI: dict[int, int] = {
    **{i: i for i in range(0x20, 0x7F)},    # 0x20-0x7E: ASCII printable
    0x80: 0x20AC, 0x82: 0x201A, 0x83: 0x0192, 0x84: 0x201E, 0x85: 0x2026,
    0x86: 0x2020, 0x87: 0x2021, 0x88: 0x02C6, 0x89: 0x2030, 0x8A: 0x0160,
    0x8B: 0x2039, 0x8C: 0x0152, 0x8E: 0x017D, 0x91: 0x2018, 0x92: 0x2019,
    0x93: 0x201C, 0x94: 0x201D, 0x95: 0x2022, 0x96: 0x2013, 0x97: 0x2014,
    0x98: 0x02DC, 0x99: 0x2122, 0x9A: 0x0161, 0x9B: 0x203A, 0x9C: 0x0153,
    0x9E: 0x017E, 0x9F: 0x0178,
    **{i: i for i in range(0xA0, 0x100)},   # 0xA0-0xFF: Latin-1 Supplement
}


def _extract_widths_from_ttf(
    font_data: bytes,
    first_char: int = 32,
    last_char: int = 255,
    encoding_map: dict[int, int] | None = None,
) -> list[int]:
    """Return PDF glyph widths (1/1000 em units) for character codes [first_char..last_char].

    Uses fontTools to read the embedded TTF's hmtx table so the PDF /Widths
    array matches the embedded font program exactly (satisfies veraPDF 7.21.5).
    Returns an empty list if fontTools is unavailable or the font can't be parsed.
    """
    try:
        from fontTools.ttLib import TTFont
        tt = TTFont(io.BytesIO(font_data))
        units_per_em = tt["head"].unitsPerEm
        hmtx = tt["hmtx"].metrics           # {glyph_name: (advance_width, lsb)}
        cmap_table = tt.getBestCmap() or {}  # {unicode_int: glyph_name}

        enc = encoding_map if encoding_map is not None else _WIN_ANSI

        widths: list[int] = []
        notdef_w = 0
        if ".notdef" in hmtx:
            notdef_w = round(hmtx[".notdef"][0] * 1000 / units_per_em)

        for char_code in range(first_char, last_char + 1):
            uni = enc.get(char_code, char_code)
            glyph_name = cmap_table.get(uni)
            if glyph_name and glyph_name in hmtx:
                w = round(hmtx[glyph_name][0] * 1000 / units_per_em)
            else:
                w = notdef_w
            widths.append(w)

        return widths
    except Exception as exc:
        log.warning("font_embed: extract_widths failed: %s", exc)
        return []


def _set_font_widths(pdf, obj, font_data: bytes) -> bool:
    """Rewrite the font dict's /FirstChar, /LastChar, /Widths from the embedded TTF.

    This makes the declared widths consistent with the embedded font program,
    satisfying veraPDF clause 7.21.5.

    Returns True only if the /Widths array was actually written. Callers MUST
    treat False as "do not embed": embedding a font program whose widths we
    cannot mirror into the font dict produces one 7.21.5 failure per glyph —
    far worse than the single not-embedded failure it was meant to fix.
    """
    try:
        first_char = int(obj.get("/FirstChar", 32))
        last_char = int(obj.get("/LastChar", 255))
        # Clamp to a reasonable printable range
        first_char = max(32, min(first_char, 255))
        last_char = max(first_char, min(last_char, 255))

        widths = _extract_widths_from_ttf(font_data, first_char, last_char)
        if widths:
            import pikepdf as _pik
            obj[_pik.Name("/FirstChar")] = first_char
            obj[_pik.Name("/LastChar")] = last_char
            obj[_pik.Name("/Widths")] = _pik.Array(widths)
            log.debug("font_embed: rewrote /Widths (%d entries, fc=%d)", len(widths), first_char)
            return True
        return False
    except Exception as exc:
        log.debug("font_embed: set_font_widths failed: %s", exc)
        return False


def _can_extract_widths(font_data: bytes) -> bool:
    """Cheap pre-flight: can we produce a correct /Widths array for this font?"""
    return bool(_extract_widths_from_ttf(font_data, 32, 33))


def _set_winansi_encoding(obj) -> None:
    """Ensure a non-symbolic TrueType font declares WinAnsiEncoding.

    veraPDF 7.21.6: all non-symbolic TrueType fonts shall have MacRomanEncoding
    or WinAnsiEncoding. Our subset/width pipeline is WinAnsi-based, so that is
    the consistent choice. Existing dict-based /Encoding (with /Differences) is
    left alone.
    """
    import pikepdf as _pik
    enc = obj.get("/Encoding")
    if enc is None or isinstance(enc, _pik.Name):
        obj[_pik.Name("/Encoding")] = _pik.Name("/WinAnsiEncoding")


def _make_tounicode_cmap(encoding_map: dict[int, int] | None = None) -> bytes:
    """Generate a ToUnicode CMap stream for WinAnsiEncoding (default) or a custom map."""
    if encoding_map is None:
        encoding_map = _WIN_ANSI
    chars = sorted((byte, uni) for byte, uni in encoding_map.items() if uni >= 0x0020)
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<00> <FF>",
        "endcodespacerange",
        f"{len(chars)} beginbfchar",
    ]
    for byte, uni in chars:
        lines.append(f"<{byte:02X}> <{uni:04X}>")
    lines += ["endbfchar", "endcmap", "CMapType currentdict end", "end"]
    return "\n".join(lines).encode("latin-1")


def embed_fonts(pdf_path: str, font_issues: list[dict] | None = None) -> tuple[int, list[str]]:
    """Embed missing fonts identified by font_check.py (or scan all if font_issues is empty).

    When font_issues is empty or None, embed ALL unembedded fonts found in the PDF.
    Returns (fonts_embedded_count, notes).  PDF is modified in-place.
    """
    font_issues = font_issues or []
    unembedded = [f for f in font_issues if f.get("issue") == "not_embedded"]
    # If a specific list was provided, filter by name; otherwise embed everything unembedded
    filter_by_name = bool(unembedded)

    try:
        import pikepdf
    except ImportError:
        log.warning("font_embed: pikepdf not available — skipping font embedding")
        return 0, []

    embedded = 0
    notes: list[str] = []
    seen: set[str] = set()  # avoid double-embedding same font

    try:
        pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)

        for page_idx, page in enumerate(pdf.pages):
            res = page.get("/Resources")
            if not res:
                log.info("font_embed: page %d has no /Resources", page_idx)
                continue
            fonts = res.get("/Font")
            if not fonts:
                log.info("font_embed: page %d has /Resources but no /Font", page_idx)
                continue
            log.info("font_embed: page %d has %d font(s)", page_idx, len(list(fonts.items())))
            for _, font_ref in fonts.items():
                try:
                    obj = font_ref.get_object() if hasattr(font_ref, "get_object") else font_ref
                    base_font = str(obj.get("/BaseFont", "")).lstrip("/")
                    if not base_font or base_font in seen:
                        continue
                    # Composite (Type0/CID) and subset fonts ("ABCDEF+Name") are
                    # untouchable: their glyph IDs, CMaps and width tables are
                    # specific to the exact embedded program. Substituting a
                    # system font or rewriting /Widths on them breaks every
                    # glyph reference (observed: 4 -> 516 veraPDF failures on a
                    # document with subset TrueType fonts).
                    subtype = str(obj.get("/Subtype", ""))
                    if subtype == "/Type0" or "+" in base_font:
                        log.debug("font_embed: skipping %r (%s) — subset/composite", base_font, subtype)
                        continue
                    # Skip if already embedded
                    desc_ref = obj.get("/FontDescriptor")
                    if not desc_ref:
                        # Standard Type 1 fonts have no descriptor — create one and embed
                        sys_path = _locate_font_with_fallback(base_font)
                        if sys_path:
                            font_data = _subset_font(sys_path)
                            if font_data and not _can_extract_widths(font_data):
                                # Fail-safe: embedding without a matching /Widths
                                # array trades 1 failure for ~77 (veraPDF 7.21.5).
                                notes.append(
                                    f"Skipped embedding {base_font}: cannot extract "
                                    "glyph widths (is fontTools installed?)"
                                )
                                log.warning("font_embed: skipping %r — widths unavailable", base_font)
                                font_data = None
                            if font_data:
                                fstream = pikepdf.Stream(pdf, font_data)
                                # Build descriptor using plain dict keys (pikepdf.Integer
                                # is not a real type — use Python ints directly)
                                new_desc = pdf.make_indirect(pikepdf.Dictionary({
                                    "/Type": pikepdf.Name("/FontDescriptor"),
                                    "/FontName": pikepdf.Name(f"/{base_font}"),
                                    "/Flags": 32,
                                    "/FontBBox": pikepdf.Array([-100, -210, 1000, 900]),
                                    "/ItalicAngle": 0,
                                    "/Ascent": 800,
                                    "/Descent": -200,
                                    "/CapHeight": 700,
                                    "/StemV": 80,
                                    "/FontFile2": fstream,
                                }))
                                obj[pikepdf.Name("/FontDescriptor")] = new_desc
                                obj[pikepdf.Name("/Subtype")] = pikepdf.Name("/TrueType")
                                _set_font_widths(pdf, obj, font_data)
                                _set_winansi_encoding(obj)  # veraPDF 7.21.6
                                seen.add(base_font)
                                embedded += 1
                                notes.append(
                                    f"Embedded {base_font} (created descriptor) "
                                    f"via {os.path.basename(sys_path)}"
                                )
                                log.info(
                                    "font_embed: created descriptor + embedded %r via %s",
                                    base_font, sys_path,
                                )
                        # Inject ToUnicode if still missing
                        if not obj.get("/ToUnicode"):
                            cmap_bytes = _make_tounicode_cmap()
                            cmap_stream = pikepdf.Stream(pdf, cmap_bytes)
                            obj[pikepdf.Name("/ToUnicode")] = cmap_stream
                            if base_font not in seen:
                                notes.append(f"ToUnicode CMap added for {base_font} (no descriptor)")
                        continue
                    desc = desc_ref.get_object() if hasattr(desc_ref, "get_object") else desc_ref
                    already_embedded = (
                        desc.get("/FontFile") or desc.get("/FontFile2") or desc.get("/FontFile3")
                    )

                    # Check if this font is in the unembedded list (or embed all if no filter)
                    matched = (not filter_by_name) or any(
                        base_font.lower() in (f.get("font_name") or "").lower()
                        for f in unembedded
                    )

                    if not already_embedded and matched:
                        # Try exact match first, then substitutes
                        sys_path = _locate_font_with_fallback(base_font)
                        if sys_path:
                            font_data = _subset_font(sys_path)
                            if font_data and not _can_extract_widths(font_data):
                                notes.append(
                                    f"Skipped embedding {base_font}: cannot extract "
                                    "glyph widths (is fontTools installed?)"
                                )
                                log.warning("font_embed: skipping %r — widths unavailable", base_font)
                                font_data = None
                            if font_data:
                                stream = pikepdf.Stream(pdf, font_data)
                                desc[pikepdf.Name("/FontFile2")] = stream
                                _set_font_widths(pdf, obj, font_data)
                                if str(obj.get("/Subtype", "")) == "/TrueType":
                                    _set_winansi_encoding(obj)  # veraPDF 7.21.6
                                seen.add(base_font)
                                embedded += 1
                                sub_name = os.path.basename(sys_path)
                                notes.append(f"Embedded {base_font} using {sub_name}")
                                log.info("font_embed: embedded %r via %s", base_font, sys_path)
                        else:
                            log.debug("font_embed: no font found for %r (tried substitutes)", base_font)
                    elif already_embedded:
                        # Font already embedded — still sync /Widths from embedded stream
                        # so re-uploaded already-remediated PDFs also pass veraPDF 7.21.5.
                        try:
                            ff_stream = (desc.get("/FontFile2") or desc.get("/FontFile")
                                         or desc.get("/FontFile3"))
                            if ff_stream is not None:
                                raw = ff_stream.get_object() if hasattr(ff_stream, "get_object") else ff_stream
                                font_bytes = bytes(raw.read_bytes())
                                _set_font_widths(pdf, obj, font_bytes)
                                log.info("font_embed: synced /Widths for already-embedded %r", base_font)
                        except Exception as _we:
                            log.warning("font_embed: width sync for already-embedded %r: %s", base_font, _we)

                    # Inject ToUnicode CMap if missing (covers both just-embedded and already-embedded)
                    if not obj.get("/ToUnicode"):
                        # Determine encoding to use for CMap
                        encoding = obj.get("/Encoding")
                        enc_name = str(encoding).lstrip("/") if encoding else ""
                        if enc_name in ("WinAnsiEncoding", "MacRomanEncoding", "") or not enc_name:
                            cmap_bytes = _make_tounicode_cmap()
                        else:
                            # Unknown encoding — use WinAnsi as best effort
                            cmap_bytes = _make_tounicode_cmap()
                        cmap_stream = pikepdf.Stream(pdf, cmap_bytes)
                        obj[pikepdf.Name("/ToUnicode")] = cmap_stream
                        if base_font not in seen:
                            notes.append(f"ToUnicode CMap added for {base_font}")

                except Exception as exc:
                    log.warning("font_embed per-font %r: %s", base_font if 'base_font' in dir() else '?', exc)

        if embedded > 0 or notes:
            pdf.save(pdf_path)

        pdf.close()
    except Exception as exc:
        log.warning("font_embed: failed: %s", exc)

    return embedded, notes
