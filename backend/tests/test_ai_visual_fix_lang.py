"""set_lang — the AI visual-fix action that corrects a document's declared
language (WCAG 3.1.1 / PDF/UA 7.2). Covers the pure BCP-47 normaliser and the
real _apply_fixes path on a pikepdf document (no API call involved)."""

from __future__ import annotations

import tempfile

import pytest

from app.ai_visual_fix import _normalise_lang, _apply_fixes

pikepdf = pytest.importorskip("pikepdf")


@pytest.mark.parametrize("raw,expected", [
    ("en", "en"),
    ("EN", "en"),
    ("en-US", "en-US"),
    ("en-us", "en-US"),
    ("FR-ca", "fr-CA"),
    ("es", "es"),
    ("zh-hant", "zh-Hant"),
    ("zh-Hant-HK", "zh-Hant-HK"),
])
def test_normalise_lang_valid(raw, expected):
    assert _normalise_lang(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "english", "e", "123", "en_US", "-en", "en-"])
def test_normalise_lang_invalid(raw):
    assert _normalise_lang(raw) is None


def _pdf_with_lang(lang):
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    if lang is not None:
        pdf.Root[pikepdf.Name("/Lang")] = pikepdf.String(lang)
    fd, path = tempfile.mkstemp(suffix=".pdf")
    import os
    os.close(fd)
    pdf.save(path)
    pdf.close()
    return path


def _lang_of(path):
    with pikepdf.open(path) as pdf:
        return str(pdf.Root.get("/Lang", ""))


def _fix(**kw):
    base = {"action": "set_lang", "target_text": "", "new_tag": "",
            "figure_number": 0, "new_alt": "", "new_title": "", "new_lang": "",
            "reason": "page is visibly Spanish"}
    base.update(kw)
    return base


def test_set_lang_corrects_wrong_language():
    path = _pdf_with_lang("en-US")
    applied, skipped = _apply_fixes(path, [_fix(new_lang="es")])
    assert _lang_of(path) == "es"
    assert len(applied) == 1 and applied[0]["action"] == "set_lang"
    assert applied[0]["lang"] == "es"
    assert skipped == []


def test_set_lang_normalises_before_writing():
    path = _pdf_with_lang("en")
    _apply_fixes(path, [_fix(new_lang="fr-ca")])
    assert _lang_of(path) == "fr-CA"


def test_set_lang_skips_when_already_correct():
    path = _pdf_with_lang("en-US")
    applied, skipped = _apply_fixes(path, [_fix(new_lang="en-US")])
    assert applied == []
    assert len(skipped) == 1 and "already" in skipped[0]["why"]


def test_set_lang_skips_invalid_tag():
    path = _pdf_with_lang("en-US")
    applied, skipped = _apply_fixes(path, [_fix(new_lang="spanish")])
    assert _lang_of(path) == "en-US"          # unchanged
    assert applied == []
    assert len(skipped) == 1 and "invalid" in skipped[0]["why"]
