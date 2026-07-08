"""
Smart contrast fixer: darken low-contrast text to the minimum shade that passes
WCAG 4.5:1 against white, preserving hue.

Algorithm:
  1. Parse each affected page's content stream
  2. Track the current fill color (rg / g / k + q/Q graphics-state stack)
  3. Before every text-show op, compute contrast against white
  4. If it fails: binary-search for the exact darkening scale that hits 4.5:1
     and inject that specific adjusted color — not black, just dark enough
  5. Already-passing colors (dark text) are left completely untouched
  6. Unknown color spaces (spot / ICC via cs/sc/scn) fall back to black
"""

from __future__ import annotations

import shutil

import pikepdf
from pikepdf import Operator

from .contrast import check_contrast

TEXT_SHOW_OPS = {"Tj", "TJ", "'", '"'}
REQUIRED_RATIO = 4.5        # WCAG 1.4.3 normal text
WHITE_LUM = 1.0             # luminance of #ffffff


# ---------------------------------------------------------------------------
# WCAG colour math
# ---------------------------------------------------------------------------

def _linearize(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(r: float, g: float, b: float) -> float:
    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)


def _contrast_vs_white(r: float, g: float, b: float) -> float:
    """WCAG contrast ratio of (r,g,b) [0–1] against white."""
    lum = _luminance(r, g, b)
    return (WHITE_LUM + 0.05) / (lum + 0.05)


def _quantize(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Snap to the 8-bit sRGB grid the renderer will actually display.

    Testing the un-quantized float color is how a fix ends up at 4.48:1 instead
    of 4.5:1 — the search converges to the exact threshold and rounding to
    #777777 pushes it just under. All pass/fail decisions and the emitted color
    must therefore live on the quantized grid.
    """
    return (round(r * 255) / 255, round(g * 255) / 255, round(b * 255) / 255)


def _darken_to_pass(
    r: float, g: float, b: float, required: float = REQUIRED_RATIO
) -> tuple[float, float, float]:
    """
    Binary-search for the minimum darkening whose *quantized* color hits
    ``required`` against white. Invisible text (1:1 ratio) is excluded upstream
    by contrast.py — not handled here.
    """
    q = _quantize(r, g, b)
    if _contrast_vs_white(*q) >= required:
        return q  # already fine once snapped to the display grid

    lo, hi = 0.0, 1.0
    for _ in range(24):               # 2^-24 ≈ 0.00006% precision
        mid = (lo + hi) / 2.0
        if _contrast_vs_white(*_quantize(r * mid, g * mid, b * mid)) >= required:
            lo = mid
        else:
            hi = mid
    out = _quantize(r * lo, g * lo, b * lo)
    # Belt-and-suspenders: if grid rounding still lands a hair short, walk each
    # channel down one 8-bit step until the quantized color passes.
    for _ in range(8):
        if _contrast_vs_white(*out) >= required:
            break
        out = tuple(max(0.0, c - 1 / 255) for c in out)  # type: ignore[assignment]
    return out


# ---------------------------------------------------------------------------
# pikepdf helpers
# ---------------------------------------------------------------------------

def _num(v: float):
    """pikepdf numeric object rounded to 4 decimal places."""
    return pikepdf.Object.parse(f"{v:.4f}".encode())


def _color_instr(r: float, g: float, b: float) -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction([_num(r), _num(g), _num(b)], Operator("rg"))


_BLACK_ZERO = None


def _black_instr() -> pikepdf.ContentStreamInstruction:
    global _BLACK_ZERO
    if _BLACK_ZERO is None:
        _BLACK_ZERO = pikepdf.Object.parse(b"0")
    return pikepdf.ContentStreamInstruction([_BLACK_ZERO, _BLACK_ZERO, _BLACK_ZERO], Operator("rg"))


# ---------------------------------------------------------------------------
# Fill-colour state tracker
# ---------------------------------------------------------------------------

class _FillColor:
    """Tracks the current fill colour through a PDF content stream."""

    def __init__(self):
        self._stack: list[tuple] = []
        self.rgb: tuple[float, float, float] = (0.0, 0.0, 0.0)  # default: black
        self.unknown: bool = False   # True for spot/ICC — can't compute RGB

    def push(self) -> None:
        self._stack.append((self.rgb, self.unknown))

    def pop(self) -> None:
        if self._stack:
            self.rgb, self.unknown = self._stack.pop()

    def apply(self, op: str, operands: list) -> None:
        if op == "rg" and len(operands) == 3:
            self.rgb = (float(operands[0]), float(operands[1]), float(operands[2]))
            self.unknown = False
        elif op == "g" and len(operands) == 1:
            gray = float(operands[0])
            self.rgb = (gray, gray, gray)
            self.unknown = False
        elif op == "k" and len(operands) == 4:
            c, m, y, k = (float(o) for o in operands)
            self.rgb = ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
            self.unknown = False
        elif op in ("cs", "sc", "scn"):
            # Spot / ICC colour — cannot reliably convert to RGB
            self.unknown = True

    def needs_fix(self, required: float = REQUIRED_RATIO) -> bool:
        if self.unknown:
            return True
        return _contrast_vs_white(*_quantize(*self.rgb)) < required

    def fix(self, required: float = REQUIRED_RATIO) -> "tuple[float,float,float] | None":
        """Return the adjusted RGB to inject, or None if already passing."""
        if not self.needs_fix(required):
            return None
        if self.unknown:
            return (0.0, 0.0, 0.0)      # black fallback for unknown spaces
        return _darken_to_pass(*self.rgb, required=required)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fix_contrast_colors(in_path: str, out_path: str) -> int:
    """
    Darken contrast-failing text to the minimum shade that passes WCAG 4.5:1.
    Returns the number of colour adjustments made (0 = nothing changed).
    """
    failures = check_contrast(in_path)
    if not failures:
        shutil.copy2(in_path, out_path)
        return 0

    failing_pages = {f["page"] for f in failures}   # 1-indexed
    fixes = 0

    with pikepdf.open(in_path) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            if (page_idx + 1) not in failing_pages:
                continue

            try:
                instructions = list(pikepdf.parse_content_stream(page))
            except Exception:
                continue

            color = _FillColor()
            in_text = False
            font_size = 12.0  # current text size via Tf — WCAG large text needs only 3:1
            out: list = []

            for instr in instructions:
                operands = list(instr.operands)
                op = str(instr.operator)

                # Graphics-state stack
                if op == "q":
                    color.push()
                elif op == "Q":
                    color.pop()

                # Track fill colour
                color.apply(op, operands)

                # Track font size (WCAG 1.4.3: >=18pt counts as large -> 3:1).
                # Bold detection is unreliable at the operator level, so 14pt
                # bold text is held to the stricter 4.5:1 — conservative.
                if op == "Tf" and len(operands) == 2:
                    try:
                        font_size = float(operands[1])
                    except (TypeError, ValueError):
                        pass

                # Text block boundaries
                if op == "BT":
                    in_text = True
                elif op == "ET":
                    in_text = False

                # Inject adjusted colour before failing text-show ops
                if op in TEXT_SHOW_OPS and in_text:
                    required = 3.0 if font_size >= 18 else REQUIRED_RATIO
                    adjusted = color.fix(required)
                    if adjusted is not None:
                        r, g, b = adjusted
                        if r == 0.0 and g == 0.0 and b == 0.0:
                            out.append(_black_instr())
                        else:
                            out.append(_color_instr(r, g, b))
                        color.rgb = adjusted
                        color.unknown = False
                        fixes += 1

                out.append(pikepdf.ContentStreamInstruction(operands, Operator(op)))

            new_data = pikepdf.unparse_content_stream(out)
            page.obj.Contents = pdf.make_stream(new_data)

        pdf.save(out_path)

    return fixes
