"""language_fix.py — inject /Lang attributes on struct elements whose text
is in a different language from the document default.

Strategy:
  1. Detect the document language from the catalog /Lang (fallback: detect
     from the first 2,000 chars of body text).
  2. Walk the structure tree, collecting text per leaf element from the
     page's text-extraction layer.
  3. For each element whose detected language differs from the doc language,
     write a /Lang attribute directly onto the struct element dictionary.

Requires:
  - pikepdf  (struct tree access)
  - fitz / PyMuPDF  (text extraction)
  - langdetect *or* langid (optional — falls back to script-range heuristics)

All changes are in-place.  Returns (fixes_applied, notes).
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Script-range heuristic (no ML lib required)
# ---------------------------------------------------------------------------
_SCRIPT_RANGES: list[tuple[str, re.Pattern]] = [
    ("ar",  re.compile(r"[؀-ۿ]")),           # Arabic / Persian
    ("he",  re.compile(r"[֐-׿]")),           # Hebrew
    ("zh",  re.compile(r"[一-鿿]")),          # CJK unified
    ("ja",  re.compile(r"[぀-ヿ]")),          # Hiragana / Katakana
    ("ko",  re.compile(r"[가-힯]")),          # Hangul
    ("ru",  re.compile(r"[Ѐ-ӿ]")),           # Cyrillic
    ("hi",  re.compile(r"[ऀ-ॿ]")),           # Devanagari
    ("el",  re.compile(r"[Ͱ-Ͽ]")),           # Greek
    ("th",  re.compile(r"[฀-๿]")),           # Thai
    ("ka",  re.compile(r"[Ⴀ-ჿ]")),           # Georgian
]


def _script_lang(text: str) -> str | None:
    """Return ISO 639-1 code if text is dominated by a non-Latin script."""
    for lang, pat in _SCRIPT_RANGES:
        hits = len(pat.findall(text))
        if hits > max(3, len(text) * 0.25):
            return lang
    return None


def _detect_lang(text: str) -> str | None:
    """Best-effort language detection.  Returns ISO 639-1 code or None."""
    text = text.strip()
    if len(text) < 8:
        return None
    # 1. Script-range heuristic (fast, no deps)
    sl = _script_lang(text)
    if sl:
        return sl
    # 2. langdetect (optional)
    try:
        from langdetect import detect
        return detect(text)
    except Exception:
        pass
    # 3. langid (optional)
    try:
        import langid
        lang, _ = langid.classify(text)
        return lang
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Struct tree walker
# ---------------------------------------------------------------------------

def _elem_text(elem, page_texts: dict[int, dict]) -> str:
    """Collect text for a struct element from page_texts (MCID → text map)."""
    parts: list[str] = []
    # Collect K (kids) — could be MCID ints, dict MCIDs, or child elements
    k = elem.get("/K")
    if k is None:
        return ""
    items = list(k) if hasattr(k, "__iter__") and not isinstance(k, (str, bytes)) else [k]
    for item in items:
        try:
            if hasattr(item, "is_integer") or isinstance(item, int):
                # Direct MCID reference
                pg_ref = elem.get("/Pg")
                if pg_ref is not None:
                    pg_idx = _page_index(pg_ref)
                    txt = page_texts.get(pg_idx, {}).get(int(item), "")
                    if txt:
                        parts.append(txt)
            elif hasattr(item, "get"):
                # Inline MCID dict: {Type: /MCR, MCID: N, Pg: ...}
                t = str(item.get("/Type", ""))
                if "MCR" in t or "/MCID" in item:
                    mcid = int(item["/MCID"])
                    pg_ref = item.get("/Pg") or elem.get("/Pg")
                    if pg_ref is not None:
                        pg_idx = _page_index(pg_ref)
                        txt = page_texts.get(pg_idx, {}).get(mcid, "")
                        if txt:
                            parts.append(txt)
        except Exception:
            continue
    return " ".join(parts)


_page_obj_to_idx: dict[int, int] = {}   # objgen → 0-based index (populated lazily)


def _page_index(pg_ref) -> int:
    """Return 0-based page index from a pikepdf indirect reference."""
    try:
        return _page_obj_to_idx.get(pg_ref.objgen, 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Main fixer
# ---------------------------------------------------------------------------

_PLACEHOLDER_ALT = {"", " ", "image", "figure", "img", "photo", "picture"}


def fix_language_tags(pdf_path: str) -> tuple[int, list[str]]:
    """Inject /Lang on struct elements whose content language differs from doc lang.

    Returns (fixes_applied, notes).  PDF modified in-place.
    """
    try:
        import pikepdf
        import fitz
    except ImportError as exc:
        log.debug("language_fix: missing dependency %s — skipped", exc)
        return 0, []

    fixes = 0
    notes: list[str] = []

    try:
        # ── 1. extract text per page → per MCID ──────────────────────────────
        doc_fitz = fitz.open(pdf_path)
        page_texts: dict[int, dict[int, str]] = {}
        all_text_parts: list[str] = []

        for pg_idx, page in enumerate(doc_fitz):
            raw = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
            mcid_map: dict[int, list[str]] = {}
            for block in raw.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        mcid = span.get("color")   # not mcid — use origin
                        # Use PyMuPDF's struct marks
                        for char in span.get("chars", []):
                            c_mcid = char.get("c", "")   # char might carry mcid
                        # Simpler: extract spans by their marked-content info
                        # PyMuPDF doesn't easily expose MCID per char in rawdict;
                        # use get_text("dict") blocks which group by span
                        text = span.get("text", "").strip()
                        if text:
                            all_text_parts.append(text)
            # Better approach: extract MCID-grouped text using get_text("words")
            # We'll use the full page text per page and do per-page detection
            page_texts[pg_idx] = {"_page_": page.get_text("text")}

        doc_fitz.close()

        # ── 2. detect doc language ────────────────────────────────────────────
        pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)

        # Build page index lookup
        _page_obj_to_idx.clear()
        for idx, page in enumerate(pdf.pages):
            try:
                _page_obj_to_idx[page.objgen] = idx
            except Exception:
                pass

        doc_lang = ""
        try:
            cat = pdf.Root
            doc_lang = str(cat.get("/Lang", "")).strip().lower().replace("-", "_")
        except Exception:
            pass

        if not doc_lang:
            sample = " ".join(all_text_parts[:200])
            detected = _detect_lang(sample)
            doc_lang = detected or "en"
            # Set it on the catalog too
            try:
                pdf.Root["/Lang"] = pikepdf.String(doc_lang)
                notes.append(f"Set catalog /Lang = {doc_lang!r} (auto-detected)")
            except Exception:
                pass

        # Normalise to primary subtag for comparison
        doc_primary = doc_lang.split("-")[0].split("_")[0]

        # ── 3. walk struct tree ───────────────────────────────────────────────
        try:
            struct_root = pdf.Root.get("/StructTreeRoot")
        except Exception:
            struct_root = None

        if struct_root is None:
            pdf.close()
            return fixes, notes

        def _walk(elem):
            nonlocal fixes
            try:
                tag = str(elem.get("/S", ""))
                # Leaf-like tags that carry actual text content
                if tag in ("/P", "/Span", "/H", "/H1", "/H2", "/H3",
                           "/H4", "/H5", "/H6", "/Link", "/Caption", "/LI"):
                    # Get page text for this element
                    pg_ref = elem.get("/Pg")
                    if pg_ref is not None:
                        pg_idx = _page_index(pg_ref)
                        page_text = page_texts.get(pg_idx, {}).get("_page_", "")
                    else:
                        page_text = ""

                    # Get element-specific text if available via ActualText or Alt
                    actual = str(elem.get("/ActualText", "")).strip()
                    text = actual or page_text[:300]   # use page text as fallback sample

                    if len(text) >= 8:
                        detected = _detect_lang(text)
                        if detected and detected.split("-")[0] != doc_primary:
                            # Check if /Lang already set
                            existing = str(elem.get("/Lang", "")).strip()
                            if not existing:
                                elem["/Lang"] = pikepdf.String(detected)
                                fixes += 1
                                notes.append(
                                    f"Tagged /{tag.lstrip('/')} with /Lang={detected!r} "
                                    f"(doc lang={doc_primary!r})"
                                )

                # Recurse into kids
                k = elem.get("/K")
                if k is not None:
                    kids = list(k) if hasattr(k, "__iter__") and not isinstance(k, (str, bytes)) else [k]
                    for kid in kids:
                        if hasattr(kid, "get"):
                            _walk(kid)
            except Exception:
                pass

        # Start from document-level kids
        try:
            top_kids = struct_root.get("/K")
            if top_kids is not None:
                items = list(top_kids) if hasattr(top_kids, "__iter__") and \
                    not isinstance(top_kids, (str, bytes)) else [top_kids]
                for item in items:
                    if hasattr(item, "get"):
                        _walk(item)
        except Exception as exc:
            log.debug("language_fix: struct walk error: %s", exc)

        if fixes > 0 or (notes and "catalog" in notes[0]):
            pdf.save(pdf_path)
            log.info("language_fix: %d /Lang tags injected", fixes)

        pdf.close()

    except Exception as exc:
        log.warning("language_fix: %s", exc)

    return fixes, notes
