"""Color contrast checker for PDFs (WCAG 1.4.3 / 1.4.11).

Uses PyMuPDF to render each page, extract text spans with foreground colors,
sample the background from the rendered pixmap, and compute WCAG contrast ratios.

Normal text requires 4.5:1; large text (>=18pt or >=14pt bold) requires 3:1.
"""

from __future__ import annotations

try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

MAX_FAILURES = 100
RENDER_SCALE = 2.0  # render at 2x for accurate background sampling


def _luminance(r: int, g: int, b: int) -> float:
    """WCAG 2.x relative luminance from 0-255 sRGB values."""
    def ch(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast_ratio(rgb1: tuple, rgb2: tuple) -> float:
    l1, l2 = _luminance(*rgb1), _luminance(*rgb2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def _int_to_rgb(color_int: int) -> tuple[int, int, int]:
    return ((color_int >> 16) & 0xFF, (color_int >> 8) & 0xFF, color_int & 0xFF)


def _to_hex(rgb: tuple) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _sample_background(
    pixmap, bbox: tuple, scale: float, fg_rgb: tuple | None = None
) -> tuple[int, int, int]:
    """Estimate the background color behind a text span.

    Samples several points WITHIN the span's bounding box rather than above it.
    Sampling above is unreliable for multi-row layouts (tables, multi-column text)
    where the row above may have a very different background color.

    Strategy: try four positions within the span; skip any that look like text
    pixels (close to fg_rgb); return the lightest surviving sample, which is
    most likely the background in a typical light-background document.
    If every sample looks like a text pixel, return the first raw sample.
    """
    x0, y0, x1, y1 = bbox
    x_center = (x0 + x1) / 2
    y_center = (y0 + y1) / 2
    span_h = max(y1 - y0, 1)

    # Candidate sampling points (page-space coordinates, y increases downward)
    candidates_pts = [
        (x_center, y0 + span_h * 0.10),   # near top of bbox (above most glyphs)
        (x_center, y0 + span_h * 0.90),   # near bottom (below most descenders)
        (x0 + (x1 - x0) * 0.05, y_center),  # far left of span, mid-height
        (x_center, y_center),              # dead center (may hit a glyph)
    ]

    def _pixel(px_pt: float, py_pt: float) -> tuple[int, int, int]:
        px = max(0, min(int(px_pt * scale), pixmap.width - 1))
        py = max(0, min(int(py_pt * scale), pixmap.height - 1))
        s = pixmap.pixel(px, py)
        return s[0], s[1], s[2]

    def _is_text_pixel(rgb: tuple[int, int, int]) -> bool:
        """Return True if this pixel looks like a text character, not background."""
        if fg_rgb is None:
            return False
        return sum(abs(int(a) - int(b)) for a, b in zip(rgb, fg_rgb)) < 40

    samples = []
    for px_pt, py_pt in candidates_pts:
        s = _pixel(px_pt, py_pt)
        if not _is_text_pixel(s):
            samples.append(s)

    if not samples:
        # All candidates hit text pixels — use dead center as last resort
        samples = [_pixel(x_center, y_center)]

    # Return the sample MOST DIFFERENT from the text color — that is the background.
    # Anti-aliased edge pixels are intermediate between fg and bg, so they have
    # smaller distance from fg than pure background pixels; this criterion
    # correctly rejects them and picks the actual background in all cases
    # (dark text on light bg, light text on dark bg, etc.).
    if fg_rgb is not None:
        return max(samples, key=lambda c: sum(abs(int(a) - int(b)) for a, b in zip(c, fg_rgb)))

    # No fg color available — fall back to lightest (works for dark-on-light).
    return max(samples, key=lambda c: c[0] + c[1] + c[2])


def check_contrast(pdf_path: str) -> list[dict]:
    """Return a list of contrast failures (up to MAX_FAILURES) in the PDF.

    Each failure is a dict with keys: page, text, fg, bg, ratio, required, bbox.
    Returns an empty list if PyMuPDF is not installed.
    """
    if not HAS_FITZ:
        return []

    failures: list[dict] = []
    doc = fitz.open(pdf_path)

    try:
        for page_num, page in enumerate(doc):
            if len(failures) >= MAX_FAILURES:
                break

            mat = fitz.Matrix(RENDER_SCALE, RENDER_SCALE)
            pixmap = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)

            blocks = page.get_text(
                "dict",
                flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP,
            ).get("blocks", [])

            for block in blocks:
                if block.get("type") != 0:  # 0 = text block
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = span.get("text", "").strip()
                        if len(text) < 2:
                            continue

                        fg_rgb = _int_to_rgb(span.get("color", 0))
                        font_size = span.get("size", 12)
                        flags = span.get("flags", 0)
                        is_bold = bool(flags & (1 << 4))
                        is_large = font_size >= 18 or (font_size >= 14 and is_bold)
                        required = 3.0 if is_large else 4.5

                        bbox = span.get("bbox", (0, 0, 0, 0))
                        bg_rgb = _sample_background(pixmap, bbox, RENDER_SCALE, fg_rgb)

                        ratio = _contrast_ratio(fg_rgb, bg_rgb)

                        # Note: we intentionally do NOT skip ratio ≤ 1.01 (same
                        # color as background). Invisible text can result from the
                        # background-cleanup step whitening a fill that had light
                        # text on it. fix_contrast will darken it to a passing
                        # shade. True "decorative" invisible text is rare in
                        # university PDFs and the fix (darkening it) is harmless.

                        if ratio < required:
                            failures.append({
                                "page": page_num + 1,
                                "text": text[:80],
                                "fg": _to_hex(fg_rgb),
                                "bg": _to_hex(bg_rgb),
                                "ratio": round(ratio, 2),
                                "required": required,
                                "bbox": list(bbox),
                            })
                            if len(failures) >= MAX_FAILURES:
                                break
    finally:
        doc.close()

    return failures
