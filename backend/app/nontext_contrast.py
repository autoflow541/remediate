"""Non-text contrast checker (WCAG 1.4.11 — Sprint 8).

WCAG 1.4.11 requires a contrast ratio of at least 3:1 for:
  • UI component boundaries (form field borders, button outlines)
  • Graphical objects that convey information (chart lines, data point markers,
    icon borders, progress bar fills)

This module uses PyMuPDF to:
  1. Find annotation rects (form fields, buttons) and sample their border colour
     against the nearest background colour.
  2. Find thin paths (stroke width ≤ 3pt) in vector content and sample stroke vs
     background — thin lines are common chart/diagram grid elements.

Returns up to 60 issues per PDF (identical to contrast.py style).
"""

from __future__ import annotations

import math


def _lum(r: int, g: int, b: int) -> float:
    def _c(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * _c(r) + 0.7152 * _c(g) + 0.0722 * _c(b)


def _ratio(c1: tuple, c2: tuple) -> float:
    l1, l2 = _lum(*c1), _lum(*c2)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def _sample_bg(page, x: float, y: float, size: float = 4.0) -> tuple[int, int, int] | None:
    """Sample the background colour at a point by rendering a tiny clip."""
    try:
        import fitz
        clip = fitz.Rect(x - size, y - size, x + size, y + size)
        pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(1, 1), colorspace=fitz.csRGB, alpha=False)
        # Take centre pixel
        cx, cy = pix.width // 2, pix.height // 2
        s = pix.sample(cx, cy)
        return (s[0], s[1], s[2])
    except Exception:
        return None


def _int_color(color_val) -> tuple[int, int, int] | None:
    """Convert a fitz colour (0-1 floats or None) to (R, G, B) 0-255."""
    if not color_val:
        return None
    try:
        if len(color_val) == 3:
            return tuple(int(c * 255) for c in color_val)
        if len(color_val) == 1:
            v = int(color_val[0] * 255)
            return (v, v, v)
    except Exception:
        pass
    return None


def _page_image_coverage(page) -> float:
    """Return fraction of page area covered by raster images (0.0–1.0)."""
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return 0.0
    img_area = 0.0
    try:
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                for r in page.get_image_rects(xref):
                    img_area += r.width * r.height
            except Exception:
                pass
    except Exception:
        pass
    return min(img_area / page_area, 1.0)


def _is_decorative_path(path, page_width: float, page_height: float) -> bool:
    """Return True when a vector path is almost certainly decorative (not informational).

    Strips:
      • Full-width/height separator rules (span ≥ 70 % of page dimension, height < 5pt)
      • Closed rectangular frames — just a border box with no data content
      • Paths whose stroke colour is nearly white (>240,240,240) — invisible ornament
    Keeps:
      • Any path that doesn't match the above — assumed informational until proven otherwise
    """
    rect = path.get("rect")
    if not rect:
        return False

    w, h = rect.width, rect.height

    # Full-width horizontal separator
    if w >= page_width * 0.7 and h < 5:
        return True
    # Full-height vertical separator
    if h >= page_height * 0.7 and w < 5:
        return True

    # Closed rectangular frame — items: move, 3 lines/curves, close
    items = path.get("items") or []
    if path.get("closePath") and len(items) <= 5 and w > 0 and h > 0:
        # Aspect ratio close to a rectangle (not a tiny corner piece)
        if min(w, h) > 5:
            return True

    # Near-white stroke — essentially invisible, purely ornamental
    color = path.get("color")
    if color:
        try:
            rgb = tuple(int(c * 255) for c in color[:3])
            if all(v >= 230 for v in rgb):
                return True
        except Exception:
            pass

    return False


def check_nontext_contrast(pdf_path: str, max_issues: int = 60) -> list[dict]:
    """Return contrast issues for non-text UI components and graphical elements.

    Relevance pass: decorative paths, full-page-width separators, closed border
    frames, near-white strokes, and paths on image-heavy pages (>60 % raster
    coverage, e.g. exported video-review PDFs) are stripped before reporting.
    Only genuinely informational graphics that fail 3:1 are returned.
    """
    try:
        import fitz
    except ImportError:
        return []

    issues: list[dict] = []

    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []

    try:
        for page_idx, page in enumerate(doc):
            if len(issues) >= max_issues:
                break
            page_num = page_idx + 1
            pw = page.rect.width
            ph = page.rect.height

            # Relevance gate: skip graphic paths on image-heavy pages
            # (screen-capture / video-review PDFs — the paths are UI chrome)
            img_coverage = _page_image_coverage(page)
            skip_graphics = img_coverage > 0.60

            # ── 1. Form field / annotation borders ─────────────────────────
            for annot in page.annots():
                if len(issues) >= max_issues:
                    break
                try:
                    atype = annot.type[1]
                    if atype not in ("Widget", "FreeText", "Square", "Circle"):
                        continue
                    rect = annot.rect
                    cy = (rect.y0 + rect.y1) / 2

                    colors = annot.colors
                    stroke = _int_color(colors.get("stroke"))
                    if not stroke:
                        continue

                    bg_x = max(0, rect.x0 - 6)
                    bg = _sample_bg(page, bg_x, cy)
                    if not bg:
                        bg = (255, 255, 255)

                    r = _ratio(stroke, bg)
                    if r < 3.0:
                        issues.append({
                            "page": page_num,
                            "type": "ui_component",
                            "component": atype,
                            "ratio": round(r, 2),
                            "required": 3.0,
                            "fg": "#{:02x}{:02x}{:02x}".format(*stroke),
                            "bg": "#{:02x}{:02x}{:02x}".format(*bg),
                            "description": (
                                f"{atype} border {r:.2f}:1 — need 3:1 (WCAG 1.4.11)"
                            ),
                        })
                except Exception:
                    continue

            # ── 2. Thin vector paths — informational only ──────────────────
            if skip_graphics:
                continue  # entire page is image chrome — nothing to flag

            try:
                paths = page.get_drawings()
                for path in paths:
                    if len(issues) >= max_issues:
                        break
                    try:
                        width = path.get("width") or 0
                        stroke = _int_color(path.get("color"))
                        if not stroke or width <= 0 or width > 3:
                            continue
                        rect = path.get("rect")
                        if not rect:
                            continue
                        span = max(rect.width, rect.height)
                        if span < 10:
                            continue

                        # Relevance filter — skip decorative paths
                        if _is_decorative_path(path, pw, ph):
                            continue

                        cx = (rect.x0 + rect.x1) / 2
                        cy = (rect.y0 + rect.y1) / 2
                        bg = _sample_bg(page, cx, cy + width + 3)
                        if not bg:
                            bg = (255, 255, 255)

                        r = _ratio(stroke, bg)
                        if r < 3.0:
                            issues.append({
                                "page": page_num,
                                "type": "graphic",
                                "component": f"line/path w={width:.1f}pt",
                                "ratio": round(r, 2),
                                "required": 3.0,
                                "fg": "#{:02x}{:02x}{:02x}".format(*stroke),
                                "bg": "#{:02x}{:02x}{:02x}".format(*bg),
                                "description": (
                                    f"Graphical line {r:.2f}:1 — need 3:1 (WCAG 1.4.11)"
                                ),
                            })
                    except Exception:
                        continue
            except Exception:
                pass

    finally:
        doc.close()

    return issues
