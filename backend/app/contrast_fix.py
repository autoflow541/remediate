"""Low-contrast auto-fix — Sprint 22 (WCAG 1.4.3) + non-text (WCAG 1.4.11).

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


# ---------------------------------------------------------------------------
# Non-text contrast auto-fix (WCAG 1.4.11)
# ---------------------------------------------------------------------------

# Matches RGB stroke colour operators in PDF content streams ("R G B RG")
_RG_OP_STROKE = re.compile(
    rb"(\d+\.\d+|\d+)\s+(\d+\.\d+|\d+)\s+(\d+\.\d+|\d+)\s+RG"
)
# Matches gray stroke operators ("G G")  — single value
_G_OP_STROKE = re.compile(rb"(\d+\.\d+|\d+)\s+G(?=\s|$)")


def _darken_to_ratio(r: float, g: float, b: float,
                     bg_lum: float, target: float = 3.0):
    """Darken RGB (0-1) to hit target contrast vs bg_lum.

    Discretion gate: if achieving 3:1 requires darkening more than 55 %
    of the original value the change would look jarring — return None to skip.
    """
    if _ratio(_lum(r, g, b), bg_lum) >= target:
        return None   # already passes, nothing to do
    for step in range(201):
        s = 1.0 - step * 0.005
        nr, ng, nb = r * s, g * s, b * s
        if _ratio(_lum(nr, ng, nb), bg_lum) >= target:
            if s < 0.45:          # would need >55 % darkening — skip
                return None
            return round(nr, 4), round(ng, 4), round(nb, 4)
    return None   # couldn't fix without going near-black


def _patch_stroke_stream(raw: bytes,
                         old_rgb: tuple[float, float, float],
                         new_rgb: tuple[float, float, float]) -> bytes:
    old_op = f"{old_rgb[0]:.4f} {old_rgb[1]:.4f} {old_rgb[2]:.4f} RG".encode()
    new_op = f"{new_rgb[0]:.4f} {new_rgb[1]:.4f} {new_rgb[2]:.4f} RG".encode()
    return raw.replace(old_op, new_op)


def _fix_widget_border(annot_obj, bg_lum: float) -> bool:
    """Darken the /MK /BC border colour of a Widget annotation in-place.

    Returns True if a change was made.
    """
    try:
        import pikepdf
        mk = annot_obj.get("/MK")
        if mk is None:
            return False
        bc = mk.get("/BC")
        if bc is None or len(bc) == 0:
            return False

        # Convert pikepdf array to float tuple (0-1)
        vals = [float(v) for v in bc]
        if len(vals) == 1:
            r = g = b = vals[0]
        elif len(vals) == 3:
            r, g, b = vals
        else:
            return False

        result = _darken_to_ratio(r, g, b, bg_lum, target=3.0)
        if result is None:
            return False

        nr, ng, nb = result
        mk["/BC"] = pikepdf.Array([pikepdf.Real(nr), pikepdf.Real(ng), pikepdf.Real(nb)])
        return True
    except Exception:
        return False


def _clear_widget_bg(annot_obj) -> bool:
    """Remove or whiten the /MK /BG fill if it's near-white but not white.

    Near-white fills (luminance ≥ 0.85) on a white page are barely perceptible
    yet register as failing contrast.  Clearing them removes the issue cleanly
    without visible design impact.

    Returns True if a change was made.
    """
    try:
        import pikepdf
        mk = annot_obj.get("/MK")
        if mk is None:
            return False
        bg = mk.get("/BG")
        if bg is None or len(bg) == 0:
            return False

        vals = [float(v) for v in bg]
        if len(vals) == 1:
            r = g = b = vals[0]
        elif len(vals) == 3:
            r, g, b = vals
        else:
            return False

        lum = _lum(r, g, b)
        # Only clear if very near-white (lum ≥ 0.85) — discretion gate
        if lum < 0.85:
            return False

        # Set to pure white — equivalent to "no fill" visually
        mk["/BG"] = pikepdf.Array([pikepdf.Real(1.0), pikepdf.Real(1.0), pikepdf.Real(1.0)])
        return True
    except Exception:
        return False


def fix_nontext_contrast(pdf_path: str,
                         nontext_issues: list[dict]) -> tuple[int, list[str]]:
    """Auto-fix WCAG 1.4.11 non-text contrast failures with discretion.

    Strategy:
    • Widget (form field) annotations:
        – Near-white background fills (lum ≥ 0.85) → whiten to pure white.
        – Border colour failing 3:1 → darken minimally (skip if >55 % change).
    • Graphic path strokes on text-heavy pages:
        – Patch RG stroke operator in content stream (same limit applies).

    Changes are applied in-place.  Returns (fixes_applied, notes).
    """
    if not nontext_issues:
        return 0, []

    try:
        import pikepdf
    except ImportError:
        return 0, []

    fixes = 0
    notes: list[str] = []

    WHITE_LUM = _lum(1.0, 1.0, 1.0)   # 1.0

    try:
        pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)

        # Group issues by (page, type)
        widget_pages: set[int] = set()
        graphic_issues: list[dict] = []
        for iss in nontext_issues:
            if iss.get("type") == "ui_component":
                widget_pages.add(iss.get("page", 0))
            elif iss.get("type") == "graphic":
                graphic_issues.append(iss)

        # ── Widget annotation fixes ─────────────────────────────────────────
        for page_idx, page in enumerate(pdf.pages):
            page_num = page_idx + 1
            if page_num not in widget_pages:
                continue
            annots = page.get("/Annots")
            if annots is None:
                continue
            for annot_ref in annots:
                try:
                    annot = annot_ref
                    if str(annot.get("/Subtype", "")) != "/Widget":
                        continue
                    # Sample background luminance from the page's bg — assume white
                    bg_lum = WHITE_LUM

                    if _clear_widget_bg(annot):
                        fixes += 1
                        notes.append(f"Page {page_num}: Widget background cleared (near-white fill removed)")

                    if _fix_widget_border(annot, bg_lum):
                        fixes += 1
                        notes.append(f"Page {page_num}: Widget border darkened to ≥3:1")
                except Exception:
                    continue

        # ── Graphic path stroke fixes (content stream) ─────────────────────
        graphic_by_page: dict[int, list[dict]] = {}
        for iss in graphic_issues:
            graphic_by_page.setdefault(iss.get("page", 0), []).append(iss)

        for page_idx, page in enumerate(pdf.pages):
            page_num = page_idx + 1
            page_issues = graphic_by_page.get(page_num, [])
            if not page_issues:
                continue

            contents = page.get("/Contents")
            if contents is None:
                continue
            streams = list(contents) if isinstance(contents, pikepdf.Array) else [contents]

            for iss in page_issues:
                fg_hex = iss.get("fg", "")
                if not fg_hex or not fg_hex.startswith("#"):
                    continue
                try:
                    fr = int(fg_hex[1:3], 16) / 255
                    fg_g = int(fg_hex[3:5], 16) / 255
                    fb = int(fg_hex[5:7], 16) / 255
                except Exception:
                    continue

                result = _darken_to_ratio(fr, fg_g, fb, WHITE_LUM, target=3.0)
                if result is None:
                    continue
                nr, ng, nb = result
                old_rgb = (round(fr, 4), round(fg_g, 4), round(fb, 4))
                new_rgb = (round(nr, 4), round(ng, 4), round(nb, 4))
                if old_rgb == new_rgb:
                    continue

                for stream in streams:
                    try:
                        raw = stream.read_bytes()
                        patched = _patch_stroke_stream(raw, old_rgb, new_rgb)
                        if patched != raw:
                            stream.write(patched)
                            fixes += 1
                            notes.append(
                                f"Page {page_num}: stroke {fg_hex} darkened "
                                f"({iss.get('ratio', 0):.2f}→≥3.0)"
                            )
                    except Exception:
                        pass

        if fixes > 0:
            pdf.save(pdf_path)
            log.info("fix_nontext_contrast: %d fixes applied", fixes)

        pdf.close()
    except Exception as exc:
        log.warning("fix_nontext_contrast: %s", exc)

    return fixes, notes
