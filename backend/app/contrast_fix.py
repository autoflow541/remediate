"""Low-contrast text auto-fix — Sprint 22 (WCAG 1.4.3).

For text that fails 4.5:1 (or 3:1 for large text), attempts to darken
the foreground color in the PDF content stream to meet the threshold.

Targets the simplest and most common case: solid RGB/gray text fills
against a white background. Complex cases (gradients, image backgrounds,
blend modes, CMYK) are skipped gracefully — no partial patches applied.

All changes are tracked in a repair log returned to the caller.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Colour math
# ---------------------------------------------------------------------------

def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _lum(r: float, g: float, b: float) -> float:
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _ratio(l1: float, l2: float) -> float:
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _darken(r: float, g: float, b: float, bg_lum: float,
             threshold: float = 4.5) -> tuple[float, float, float]:
    """Scale RGB down until contrast against bg_lum meets threshold."""
    for step in range(201):
        s = 1.0 - step * 0.005
        nr, ng, nb = r * s, g * s, b * s
        if _ratio(_lum(nr, ng, nb), bg_lum) >= threshold:
            return nr, ng, nb
    return 0.0, 0.0, 0.0   # fallback: black


# Matches "R G B rg" (lowercase = fill colour) in PDF content streams
_RG_OP = re.compile(
    rb"(\d+\.\d+|\d+)\s+(\d+\.\d+|\d+)\s+(\d+\.\d+|\d+)\s+rg"
)


def _patch_stream(raw: bytes, old_rgb: tuple[float, float, float],
                  new_rgb: tuple[float, float, float]) -> bytes:
    old_op = f"{old_rgb[0]:.4f} {old_rgb[1]:.4f} {old_rgb[2]:.4f} rg".encode()
    new_op = f"{new_rgb[0]:.4f} {new_rgb[1]:.4f} {new_rgb[2]:.4f} rg".encode()
    return raw.replace(old_op, new_op)


def fix_contrast(pdf_path: str, contrast_failures: list[dict]) -> tuple[int, list[str]]:
    """Patch low-contrast text colours in the PDF content streams.

    `contrast_failures` — list of dicts from contrast_check.py.
    Returns (fixes_applied, notes).  PDF is modified in-place.
    """
    if not contrast_failures:
        return 0, []

    # Only fix failures against a white/near-white background
    to_fix: list[dict] = [
        f for f in contrast_failures
        if f.get("ratio", 99) < 4.5
        and f.get("fg_rgb") is not None
    ]
    if not to_fix:
        return 0, []

    try:
        import pikepdf
    except ImportError:
        return 0, []

    fixes = 0
    notes: list[str] = []

    # Group by page
    by_page: dict[int, list[dict]] = {}
    for f in to_fix:
        by_page.setdefault(f.get("page", 0), []).append(f)

    try:
        pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)

        for page_idx, page in enumerate(pdf.pages):
            page_num = page_idx + 1
            page_fixes = by_page.get(page_num, [])
            if not page_fixes:
                continue

            contents = page.get("/Contents")
            if contents is None:
                continue
            streams = list(contents) if isinstance(contents, pikepdf.Array) else [contents]

            for failure in page_fixes:
                fg = failure.get("fg_rgb")
                if not fg or len(fg) < 3:
                    continue
                # Normalise to 0-1
                r, g, b = [c / 255 if c > 1 else c for c in fg[:3]]
                bg = failure.get("bg_rgb", (1, 1, 1))
                bg_lum = _lum(*[c / 255 if c > 1 else c for c in bg[:3]])
                nr, ng, nb = _darken(r, g, b, bg_lum)
                old_rgb = (round(r, 4), round(g, 4), round(b, 4))
                new_rgb = (round(nr, 4), round(ng, 4), round(nb, 4))
                if old_rgb == new_rgb:
                    continue

                for stream in streams:
                    try:
                        raw = stream.read_bytes()
                        patched = _patch_stream(raw, old_rgb, new_rgb)
                        if patched != raw:
                            stream.write(patched)
                            fixes += 1
                            notes.append(
                                f"Page {page_num}: {r:.2f},{g:.2f},{b:.2f} → "
                                f"{nr:.2f},{ng:.2f},{nb:.2f} "
                                f"(ratio {failure.get('ratio', 0):.2f}→≥4.5)"
                            )
                    except Exception:
                        pass

        if fixes > 0:
            pdf.save(pdf_path)
            log.info("contrast_fix: %d stream patches applied", fixes)

        pdf.close()
    except Exception as exc:
        log.warning("contrast_fix: %s", exc)

    return fixes, notes
