"""Remove decorative colored background fills that cause false contrast failures.

Many branded PDFs have a colored band/rectangle behind heading text.
The contrast checker sees the text color against that colored background and
correctly reports a failure — but the fix_contrast fixer checks against white,
so it doesn't help.  This module addresses that by whitening large colored
fills in the content stream *before* contrast checking runs.

Strategy:
  - Parse each page's content stream.
  - Track fill color state (rg / g / k / q / Q).
  - When a fill operation (f, F, f*, B, B*, b, b*) follows a large rectangle
    path (re) with a visually chromatic fill color, inject "1 1 1 rg" right
    before the fill op so the rectangle is painted white.

"Large" = area >= MIN_BACKGROUND_AREA pt².
"Chromatic" = not near-black (lum < BLACK_THRESHOLD) and not near-white
             (lum > WHITE_THRESHOLD) — plain grays are left alone.

Result: text originally on a colored band is now on white, which
fix_contrast's binary search can then darken correctly.
"""

from __future__ import annotations

import shutil

import pikepdf
from pikepdf import Operator

MIN_BACKGROUND_AREA = 400.0   # pt² — threshold for a "background" rectangle
BLACK_THRESHOLD = 0.05        # luminance — leave very dark fills alone
WHITE_THRESHOLD = 0.95        # luminance — already white, skip

FILL_OPS = {"f", "F", "f*", "B", "B*", "b", "b*"}


def _linearize(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(r: float, g: float, b: float) -> float:
    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)


def _is_chromatic(r: float, g: float, b: float) -> bool:
    """True if the color is a mid-range, noticeably chromatic fill."""
    lum = _luminance(r, g, b)
    if lum <= BLACK_THRESHOLD or lum >= WHITE_THRESHOLD:
        return False
    gray = (r + g + b) / 3.0
    chroma = max(abs(r - gray), abs(g - gray), abs(b - gray))
    return chroma > 0.04  # small threshold catches even muted brand colors


def _rect_area(operands: list) -> float:
    if len(operands) < 4:
        return 0.0
    try:
        return abs(float(operands[2])) * abs(float(operands[3]))
    except (ValueError, TypeError):
        return 0.0


class _FillState:
    """Minimal fill-color tracker (rg / g / k + q/Q stack)."""

    def __init__(self) -> None:
        self._stack: list[tuple] = []
        self.rgb: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def push(self) -> None:
        self._stack.append(self.rgb)

    def pop(self) -> None:
        if self._stack:
            self.rgb = self._stack.pop()

    def apply(self, op: str, operands: list) -> None:
        if op == "rg" and len(operands) == 3:
            self.rgb = (float(operands[0]), float(operands[1]), float(operands[2]))
        elif op == "g" and len(operands) == 1:
            g = float(operands[0])
            self.rgb = (g, g, g)
        elif op == "k" and len(operands) == 4:
            c, m, y, k = (float(o) for o in operands)
            self.rgb = ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
        elif op == "RG" and len(operands) == 3:
            pass  # stroke only — ignore
        elif op == "G" and len(operands) == 1:
            pass  # stroke only — ignore
        elif op == "K" and len(operands) == 4:
            pass  # stroke only — ignore


def _white_instr() -> pikepdf.ContentStreamInstruction:
    one = pikepdf.Object.parse(b"1")
    return pikepdf.ContentStreamInstruction([one, one, one], Operator("rg"))


def clean_background_fills(in_path: str, out_path: str) -> int:
    """Whiten large chromatic background fills.  Returns number of fills changed."""
    changes = 0

    with pikepdf.open(in_path) as pdf:
        for page in pdf.pages:
            try:
                instructions = list(pikepdf.parse_content_stream(page))
            except Exception:
                continue

            state = _FillState()
            out: list = []
            pending_large_rect = False
            modified = False

            for instr in instructions:
                operands = list(instr.operands)
                op = str(instr.operator)

                if op == "q":
                    state.push()
                elif op == "Q":
                    state.pop()

                state.apply(op, operands)

                # Any path construction other than 're' resets the rect flag.
                if op in {"m", "l", "c", "v", "y", "h"}:
                    pending_large_rect = False

                if op == "re":
                    area = _rect_area(operands)
                    pending_large_rect = area >= MIN_BACKGROUND_AREA
                    out.append(pikepdf.ContentStreamInstruction(operands, Operator(op)))
                    continue

                if op in FILL_OPS and pending_large_rect and _is_chromatic(*state.rgb):
                    # Inject white fill right before this fill operator.
                    # The 're' path is already in `out`; the 'f' uses current
                    # graphics-state fill color, so setting it here is correct.
                    out.append(_white_instr())
                    changes += 1
                    modified = True
                    pending_large_rect = False
                    out.append(pikepdf.ContentStreamInstruction(operands, Operator(op)))
                    continue

                # Any non-re, non-fill op resets rect tracking.
                if op not in FILL_OPS:
                    pending_large_rect = False

                out.append(pikepdf.ContentStreamInstruction(operands, Operator(op)))

            if modified:
                new_data = pikepdf.unparse_content_stream(out)
                page.obj.Contents = pdf.make_stream(new_data)

        pdf.save(out_path)

    return changes
