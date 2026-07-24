"""WCAG 1.4.3 contrast math — regression tests.

Guards the quantization fix: the darkening search must judge and emit colours on
the 8-bit sRGB grid, because a float colour that meets 4.5:1 exactly can round
to a shade that measures 4.48:1 on screen (the real bug this locks down).
"""

from __future__ import annotations

from app.fix_contrast import (
    _contrast_vs_white,
    _quantize,
    _darken_to_pass,
    REQUIRED_RATIO,
)


def test_quantize_snaps_to_8bit_grid():
    r, g, b = _quantize(0.5, 0.5, 0.5)
    assert r == round(0.5 * 255) / 255
    # already-on-grid values are unchanged
    assert _quantize(0.0, 1.0, 0.0) == (0.0, 1.0, 0.0)


def test_contrast_vs_white_endpoints():
    assert _contrast_vs_white(1, 1, 1) == 1.0          # white on white
    assert _contrast_vs_white(0, 0, 0) == 21.0         # black on white


def test_darken_result_passes_on_the_quantized_grid():
    # A mid grey that fails 4.5:1; the result, once snapped to 8-bit, must pass.
    for grey in (0.6, 0.55, 0.5, 0.47):
        out = _darken_to_pass(grey, grey, grey)
        q = _quantize(*out)
        assert _contrast_vs_white(*q) >= REQUIRED_RATIO, (
            f"grey={grey} -> {q} = {_contrast_vs_white(*q):.3f}:1 (< {REQUIRED_RATIO})")


def test_darken_leaves_passing_colours_untouched():
    # Black already passes; must not be altered.
    assert _darken_to_pass(0.0, 0.0, 0.0) == (0.0, 0.0, 0.0)


def test_large_text_uses_3to1_threshold():
    # A grey that fails 4.5 but passes 3:1 should be left effectively as-is
    # (only snapped) when the caller asks for the large-text threshold.
    grey = 0.45
    out = _darken_to_pass(grey, grey, grey, required=3.0)
    assert _contrast_vs_white(*_quantize(*out)) >= 3.0
