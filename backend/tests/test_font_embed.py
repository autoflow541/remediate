"""Unit tests for font-substitute resolution (ROADMAP #4).

Pure-logic tests — no system fonts, no veraPDF. They pin the behaviour that a
base-14 / unmapped font name resolves to the correct serif/sans/mono substitute
with the right weight/style, so embedding can satisfy veraPDF 7.21.4.1 instead
of silently failing (the bare-"Times" gap these tests were written for).
"""
from app.font_embed import (
    _normalise_name,
    _resolve_substitutes,
    _base_family,
    _style_of,
    _FLAG_FORCE_BOLD,
    _FLAG_ITALIC,
)


def _first_serif(subs):
    return any("Serif" in s for s in subs)


def _first_sans(subs):
    return any("Sans" in s for s in subs)


def _first_mono(subs):
    return any("Mono" in s for s in subs)


def test_normalise_strips_subset_prefix_and_punctuation():
    assert _normalise_name("ABCDEF+Times-Roman") == "timesroman"
    assert _normalise_name("Helvetica-BoldOblique") == "helveticaboldoblique"


def test_bare_times_resolves_to_serif():
    # The bug: bare "Times" had no map key and never embedded.
    subs = _resolve_substitutes("Times")
    assert _first_serif(subs)
    assert "Liberation Serif" in subs


def test_bare_families_pick_the_right_script():
    assert _first_sans(_resolve_substitutes("Helvetica"))
    assert _first_sans(_resolve_substitutes("Arial"))
    assert _first_mono(_resolve_substitutes("Courier"))
    assert _first_serif(_resolve_substitutes("Georgia"))


def test_curated_map_is_still_used_first():
    subs = _resolve_substitutes("Times-Bold")
    assert subs[0] == "Liberation Serif Bold"  # from _TYPE1_SUBSTITUTES


def test_weight_and_style_from_name():
    subs = _resolve_substitutes("Times-BoldItalic")
    assert any("Serif Bold Italic" in s for s in subs)
    subs_it = _resolve_substitutes("Times-Italic")
    assert any(s.endswith("Serif Italic") for s in subs_it)


def test_weight_style_from_descriptor_flags():
    # Ambiguous name, weight/style only in /Flags + /ItalicAngle.
    bold = _resolve_substitutes("Times", flags=_FLAG_FORCE_BOLD)
    assert any(s.endswith("Serif Bold") for s in bold)
    ital = _resolve_substitutes("Times", flags=_FLAG_ITALIC)
    assert any(s.endswith("Serif Italic") for s in ital)
    ital_angle = _resolve_substitutes("Times", italic_angle=-12.0)
    assert any(s.endswith("Serif Italic") for s in ital_angle)


def test_style_helpers():
    assert _style_of("timesbold", 0, 0) == (True, False)
    assert _style_of("timesitalic", 0, 0) == (False, True)
    assert _style_of("times", _FLAG_FORCE_BOLD | _FLAG_ITALIC, 0) == (True, True)
    assert _base_family("courier") == ["Liberation Mono", "DejaVu Sans Mono", "FreeMono"]


def test_plain_family_is_a_final_fallback():
    subs = _resolve_substitutes("Times-Bold")
    assert "Liberation Serif" in subs  # bare family present after styled entries
