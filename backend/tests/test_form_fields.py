"""form_fields._slug_to_label — turning raw AcroForm /T field names into
human-readable accessible labels (WCAG 4.1.2 Name, Role, Value)."""

from __future__ import annotations

import pytest

from app.form_fields import _slug_to_label


@pytest.mark.parametrize("raw,expected", [
    ("firstName", "First Name"),
    ("emailAddress", "Email Address"),
    ("date_of_birth", "Date of Birth"),
    ("terms_and_conditions", "Terms and Conditions"),
    ("field_3", "Field 3"),
    ("cb_agree_terms", "Agree Terms"),
    ("ssn", "SSN"),
    ("home-zip", "Home ZIP"),
    ("of_counsel", "Of Counsel"),          # minor word leads -> capitalised
])
def test_slug_to_label(raw, expected):
    assert _slug_to_label(raw) == expected


def test_empty_falls_back_to_titlecase():
    # Nothing left after stripping separators -> title-case the raw input.
    assert _slug_to_label("___") == "___".title()
