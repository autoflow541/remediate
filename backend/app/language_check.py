"""Language tagging checker — WCAG 3.1.1 + 3.1.2 / PDF/UA-1 §7.2.

Checks two things:

  1. Document-level /Lang is present on the catalog (WCAG 3.1.1).
     This is already handled by patch_pdf, but we still audit it so the
     check shows up in the report even when remediation hasn't been run yet.

  2. Language changes within the content are tagged with a /Lang attribute
     on the relevant Span struct element (WCAG 3.1.2 / PDF/UA §7.2).
     We use heuristic language detection (langdetect or langid if available;
     otherwise we look for common Unicode script-range switches and explicit
     foreign-word lists) to flag suspected language changes.

Returns a list of issue dicts.  Relies on the optional `langdetect` package
for accurate per-span detection; degrades gracefully if it isn't installed.
"""

from __future__ import annotations

import re
from typing import Any

# ── Script-range heuristics (no third-party lib required) ────────────────────
# These detect obvious non-Latin scripts that almost certainly need a /Lang tag.

_SCRIPT_RANGES: list[tuple[str, re.Pattern]] = [
    ("Arabic/Persian", re.compile(r"[؀-ۿݐ-ݿ]")),
    ("Hebrew", re.compile(r"[֐-׿]")),
    ("CJK (Chinese/Japanese/Korean)", re.compile(r"[一-鿿぀-ヿ가-힯]")),
    ("Cyrillic", re.compile(r"[Ѐ-ӿ]")),
    ("Devanagari", re.compile(r"[ऀ-ॿ]")),
    ("Greek", re.compile(r"[Ͱ-Ͽ]")),
    ("Thai", re.compile(r"[฀-๿]")),
    ("Georgian", re.compile(r"[Ⴀ-ჿ]")),
    ("Armenian", re.compile(r"[԰-֏]")),
    ("Hangul", re.compile(r"[가-힯ᄀ-ᇿ]")),
    ("Tibetan", re.compile(r"[ༀ-࿿]")),
    ("Khmer", re.compile(r"[ក-៿]")),
    ("Ethiopic", re.compile(r"[ሀ-፿]")),
]

# Minimum span text length to bother language-checking
_MIN_LEN = 15
# Cap for performance
_MAX_LEN = 600


def _detect_script_mismatch(text: str, doc_lang: str | None) -> str | None:
    """Return a script name if the text contains a non-Latin script.

    If doc_lang already indicates a non-Latin script language, we skip those
    ranges so we only flag CHANGES from the document language.
    """
    # Detect primary doc script from lang tag (e.g. "zh" → CJK expected)
    cjk_langs = {"zh", "ja", "ko", "zh-hans", "zh-hant"}
    arabic_langs = {"ar", "fa", "ur"}
    heb_langs = {"he", "yi"}
    cyr_langs = {"ru", "uk", "bg", "sr", "be", "mk"}

    doc = (doc_lang or "").lower()

    for script_name, pattern in _SCRIPT_RANGES:
        if pattern.search(text):
            # Skip if this is the expected script for the document language
            if "CJK" in script_name and any(doc.startswith(l) for l in cjk_langs):
                continue
            if "Arabic" in script_name and any(doc.startswith(l) for l in arabic_langs):
                continue
            if "Hebrew" in script_name and any(doc.startswith(l) for l in heb_langs):
                continue
            if "Cyrillic" in script_name and any(doc.startswith(l) for l in cyr_langs):
                continue
            return script_name
    return None


def _try_langdetect(text: str) -> str | None:
    """Return ISO 639-1 language code via langdetect, or None."""
    try:
        from langdetect import detect  # type: ignore
        return detect(text)
    except Exception:
        return None


def _obj_tag(obj) -> str:
    try:
        return str(obj.get("/S", "")).lstrip("/")
    except Exception:
        return ""


def _obj_lang(obj) -> str | None:
    """Return the /Lang attribute if set on this struct element."""
    try:
        lang = obj.get("/Lang")
        if lang is not None:
            return str(lang).strip()
    except Exception:
        pass
    return None


def _obj_text(obj) -> str:
    """Extract /ActualText or /Alt content."""
    for key in ("/ActualText", "/Alt"):
        try:
            val = obj.get(key)
            if val:
                return str(val)
        except Exception:
            pass
    return ""


def _page_of(obj, page_map: dict) -> int | None:
    try:
        pg = obj.get("/Pg")
        if pg is not None:
            return page_map.get(id(pg))
    except Exception:
        pass
    return None


def _walk_spans(obj, page_map: dict, doc_lang: str | None,
                issues: list, seen: set, _depth: int = 0) -> None:
    """Walk struct tree; flag Span/P/Lbl elements with untranslated text."""
    if _depth > 80:
        return

    import pikepdf

    tag = _obj_tag(obj)
    element_lang = _obj_lang(obj)
    text = _obj_text(obj)

    if tag in ("Span", "P", "H", "H1", "H2", "H3", "H4", "H5", "H6", "Lbl") and text:
        t = text.strip()
        if _MIN_LEN <= len(t) <= _MAX_LEN and element_lang is None:
            page = _page_of(obj, page_map)

            # 1. Script-range heuristic (catches non-Latin scripts)
            script = _detect_script_mismatch(t, doc_lang)
            if script:
                key = (page, t[:40].lower())
                if key not in seen:
                    seen.add(key)
                    issues.append({
                        "type": "suspected_language_change",
                        "page": page,
                        "tag": tag,
                        "text": t[:100],
                        "detectedScript": script,
                        "description": (
                            f"Text appears to contain {script} script but the containing "
                            f"'{tag}' struct element has no /Lang attribute. "
                            "WCAG 3.1.2 requires language changes to be programmatically "
                            "identifiable. (PDF/UA §7.2)"
                        ),
                        "suggestion": (
                            f"Add a /Lang attribute (e.g. 'ar', 'zh', 'ru') to the Span "
                            "struct element that wraps this foreign-language text."
                        ),
                    })
                    return  # don't also flag with langdetect

            # 2. langdetect (optional — fires for Latin-script languages)
            if doc_lang:
                detected = _try_langdetect(t)
                if detected and not doc_lang.lower().startswith(detected.lower()):
                    key = (page, t[:40].lower())
                    if key not in seen:
                        seen.add(key)
                        issues.append({
                            "type": "suspected_language_change",
                            "page": page,
                            "tag": tag,
                            "text": t[:100],
                            "detectedScript": detected,
                            "description": (
                                f"Text may be in '{detected}' (document language: '{doc_lang}') "
                                f"but the '{tag}' element has no /Lang attribute. "
                                "WCAG 3.1.2 requires language changes to be marked. (PDF/UA §7.2)"
                            ),
                            "suggestion": (
                                f"Wrap foreign-language text in a Span struct element "
                                f"with Lang='{detected}'."
                            ),
                        })

    try:
        kids = obj.get("/K")
    except Exception:
        return
    if kids is None:
        return
    if isinstance(kids, pikepdf.Array):
        for kid in kids:
            try:
                if isinstance(kid, (pikepdf.Dictionary, pikepdf.Object)):
                    _walk_spans(kid, page_map, doc_lang, issues, seen, _depth + 1)
            except Exception:
                continue
    elif isinstance(kids, pikepdf.Dictionary):
        _walk_spans(kids, page_map, doc_lang, issues, seen, _depth + 1)


def check_language(pdf_path: str) -> list[dict[str, Any]]:
    """Return language-tagging issues for *pdf_path*.

    Each issue dict:
        type            — 'missing_doc_lang' or 'suspected_language_change'
        page            — 1-based page number (or None for doc-level)
        description     — human-readable explanation
        suggestion      — remediation advice
        text            — excerpt of flagged text (for language-change issues)
        detectedScript  — script name or lang code detected (for changes)
        tag             — struct element tag (for language-change issues)
    """
    try:
        import pikepdf
    except ImportError:
        return []

    try:
        pdf = pikepdf.open(pdf_path)
    except Exception:
        return []

    issues: list[dict] = []

    # ── Issue 1: Missing document /Lang ───────────────────────────────────────
    doc_lang: str | None = None
    try:
        lang_val = pdf.Root.get("/Lang")
        if lang_val is None:
            issues.append({
                "type": "missing_doc_lang",
                "page": None,
                "description": (
                    "The PDF catalog has no /Lang entry. Screen readers cannot "
                    "select the correct speech synthesizer voice without a declared "
                    "document language. (WCAG 3.1.1 / PDF/UA §7.2)"
                ),
                "suggestion": (
                    "Add /Lang to the PDF catalog, e.g. 'en-US' for American English. "
                    "This is auto-fixed by the /patch endpoint."
                ),
            })
        else:
            doc_lang = str(lang_val).strip()
    except Exception:
        pass

    # ── Issue 2: Language changes without /Lang on element ───────────────────
    page_map: dict[int, int] = {}
    try:
        for i, page in enumerate(pdf.pages):
            page_map[id(page.obj)] = i + 1
    except Exception:
        pass

    seen: set = set()
    try:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is not None:
            _walk_spans(struct_root, page_map, doc_lang, issues, seen)
    except Exception:
        pass
    finally:
        pdf.close()

    return issues
