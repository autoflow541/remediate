"""alt_quality._is_generic — the pure logic that flags non-descriptive Figure
alt text (WCAG 1.1.1). Filenames and too-short strings are the common failures."""

from __future__ import annotations

import pytest

from app.alt_quality import _is_generic


@pytest.mark.parametrize("alt", [
    "",
    "   ",
    "image",
    "Photo",
    "figure 1",
    "Figure 12.",
    "img_007",
    "img001",
    "logo",
    "graphic",
    # Filename-as-alt — the headline gap this checker used to miss.
    "banner.png",
    "DSC_0421.JPG",
    "screen-shot.jpeg",
    "my logo.gif",
    "diagram.svg",
    "icon.webp",
    # Too short to describe anything.
    "a",
    "x",
    "12",
    ".",
])
def test_flags_non_descriptive(alt):
    assert _is_generic(alt) is True


@pytest.mark.parametrize("alt", [
    "Bar chart showing quarterly revenue growth from 2019 to 2024",
    "The company logo, a blue heron in flight",
    "A red stop sign at an intersection",
    "Portrait of Ada Lovelace seated at a desk",
    # Mentions a file type mid-sentence but is genuinely descriptive.
    "Screenshot of the settings page with the export button highlighted",
    "Map of Oregon",  # short but meaningful (> 2 chars, not a filename)
])
def test_accepts_descriptive(alt):
    assert _is_generic(alt) is False


def test_filename_with_spaces_in_sentence_not_flagged():
    # A real sentence that happens to contain a filename token should not be
    # mistaken for a bare filename (the regex requires the whole string match).
    assert _is_generic("Upload dialog showing photo.png selected") is False
