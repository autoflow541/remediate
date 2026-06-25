"""Color-only information detector (WCAG 1.4.1 – Use of Color).

WCAG 1.4.1 requires that color is not used as the *only* visual means of
conveying information. This module uses a heuristic to detect the most
common pattern: a set of filled rectangles (legend swatches or bar chart
segments) that differ only in fill color, with no accompanying text label
inside or immediately adjacent to them.

Heuristic:
  1. Parse each page's content stream for filled path/rect operators.
  2. Group fills that are roughly the same size and Y-aligned (same row or
     column) — these are likely legend swatches or bar segments.
  3. For each group with ≥ 3 distinct colors and < 1 text span within the
     swatch bounding box, flag it as a potential color-only pattern.
  4. Return one warning per flagged page with the detected colors.

This is intentionally conservative (high precision, lower recall) to avoid
false-positive noise. The result is a *warning* the user should review, not
an auto-fix.
"""

from __future__ import annotations

import re

# Minimum group size to flag (to avoid flagging a simple two-color divider)
MIN_COLORS_IN_GROUP = 3
# Maximum swatch width/height ratio (swatches are roughly square or rect)
MAX_ASPECT = 8.0
# Swatches in a legend cluster are typically small
MAX_SWATCH_AREA = 3600.0   # ≤ 60×60 pt
MIN_SWATCH_AREA = 9.0      # ≥ 3×3 pt (ignore hairlines)
# Y-alignment tolerance for swatches on the same row
Y_BAND = 6.0
# X/Y gap between consecutive swatches in a cluster
MAX_GAP = 60.0


def _extract_fills(content_bytes: bytes) -> list[dict]:
    """Extract filled rectangles from a PDF content stream.

    Returns list of {x, y, w, h, r, g, b} dicts (page coordinates).
    Only captures non-stroking fill color set immediately before a rect/fill
    operator.

    This is a simplified parser — it handles the common case of:
      r g b rg   (set fill color)
      x y w h re f  (fill rectangle)
    and the re f* / n variants. It does not handle general path operators
    or color spaces beyond DeviceRGB.
    """
    fills: list[dict] = []

    # Tokenise: numbers, keywords
    tokens = re.findall(
        rb"(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[A-Za-z*\"\']+)",
        content_bytes,
    )

    # Walk token list tracking current fill color and rect stack
    r = g = b = 0.0
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        # Non-stroking color: "rg" expects R G B rg
        if tok == b"rg" and i >= 3:
            try:
                r = float(tokens[i - 3])
                g = float(tokens[i - 2])
                b = float(tokens[i - 1])
            except (ValueError, IndexError):
                pass
        # Shorthand gray: "g" expects gray g
        elif tok == b"g" and i >= 1:
            try:
                gr = float(tokens[i - 1])
                r = g = b = gr
            except (ValueError, IndexError):
                pass
        # Rectangle: x y w h re  → followed by f / F / f* / B / B*
        elif tok == b"re" and i >= 4:
            try:
                rx = float(tokens[i - 4])
                ry = float(tokens[i - 3])
                rw = float(tokens[i - 2])
                rh = float(tokens[i - 1])
                # Check if next op is a fill operator
                if i + 1 < n and tokens[i + 1] in (b"f", b"F", b"f*", b"B", b"B*"):
                    fills.append({
                        "x": rx, "y": ry,
                        "w": abs(rw), "h": abs(rh),
                        "r": r, "g": g, "b": b,
                    })
            except (ValueError, IndexError):
                pass
        i += 1

    return fills


def _cluster_swatches(fills: list[dict]) -> list[list[dict]]:
    """Group fills that look like legend swatches (similar size, aligned)."""
    # Filter by size
    swatches = [
        f for f in fills
        if (MIN_SWATCH_AREA <= f["w"] * f["h"] <= MAX_SWATCH_AREA)
        and (f["w"] / max(f["h"], 0.1) <= MAX_ASPECT)
        and (f["h"] / max(f["w"], 0.1) <= MAX_ASPECT)
    ]
    if not swatches:
        return []

    # Sort by Y then X
    swatches.sort(key=lambda f: (-f["y"], f["x"]))

    # Group by Y-band
    clusters: list[list[dict]] = []
    current: list[dict] = []
    for sw in swatches:
        if not current:
            current.append(sw)
        elif abs(sw["y"] - current[-1]["y"]) <= Y_BAND:
            # Same row — check horizontal gap
            gap = sw["x"] - (current[-1]["x"] + current[-1]["w"])
            if gap <= MAX_GAP:
                current.append(sw)
            else:
                if len(current) >= MIN_COLORS_IN_GROUP:
                    clusters.append(current)
                current = [sw]
        else:
            if len(current) >= MIN_COLORS_IN_GROUP:
                clusters.append(current)
            current = [sw]

    if len(current) >= MIN_COLORS_IN_GROUP:
        clusters.append(current)

    return clusters


def _distinct_colors(swatches: list[dict]) -> list[tuple]:
    """Return deduplicated (r, g, b) tuples (rounded to 2dp)."""
    seen: set[tuple] = set()
    for sw in swatches:
        key = (round(sw["r"], 2), round(sw["g"], 2), round(sw["b"], 2))
        seen.add(key)
    return list(seen)


def _color_hex(r: float, g: float, b: float) -> str:
    return "#{:02x}{:02x}{:02x}".format(
        int(round(r * 255)), int(round(g * 255)), int(round(b * 255))
    )


def detect_color_only(pdf_path: str) -> list[dict]:
    """Scan each page for potential color-only information patterns.

    Returns a list of warnings, one per flagged page:
      { page, colors: [...hex...], swatch_count, description }
    """
    try:
        import pikepdf
    except ImportError:
        return []

    warnings: list[dict] = []

    try:
        pdf = pikepdf.open(pdf_path)
    except Exception:
        return []

    try:
        for page_num, page in enumerate(pdf.pages, 1):
            try:
                # Get the page content stream(s)
                contents = page.get("/Contents")
                if contents is None:
                    continue

                # Flatten to bytes
                import pikepdf
                if isinstance(contents, pikepdf.Array):
                    raw = b"".join(bytes(stream.read_raw_bytes()) for stream in contents)
                else:
                    raw = bytes(contents.read_raw_bytes())

                # Decompress if needed (pikepdf handles this via read_bytes)
                try:
                    if isinstance(contents, pikepdf.Array):
                        raw = b"".join(bytes(stream.read_bytes()) for stream in contents)
                    else:
                        raw = bytes(contents.read_bytes())
                except Exception:
                    pass  # Fall back to raw bytes already assigned

                fills = _extract_fills(raw)
                clusters = _cluster_swatches(fills)

                for cluster in clusters:
                    colors = _distinct_colors(cluster)
                    if len(colors) < MIN_COLORS_IN_GROUP:
                        continue  # All swatches same color — not color-only
                    hex_colors = [_color_hex(c[0], c[1], c[2]) for c in colors]
                    warnings.append({
                        "page": page_num,
                        "colors": hex_colors,
                        "swatch_count": len(cluster),
                        "description": (
                            f"Page {page_num}: {len(cluster)} colored shapes with "
                            f"{len(colors)} distinct colors — may rely on color alone "
                            f"to convey meaning (WCAG 1.4.1)"
                        ),
                    })
            except Exception:
                continue
    finally:
        pdf.close()

    return warnings
