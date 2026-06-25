"""Sensory-characteristics checker (WCAG 1.3.3).

WCAG 1.3.3 requires that instructions for understanding content do not rely
solely on sensory characteristics — shape, color, size, visual location,
orientation, or sound.

This module scans the struct tree of a tagged PDF for text content containing
common sensory-only reference patterns (e.g. "click the red button", "see the
box on the right", "refer to the diagram above").  It returns advisory warnings
that must be reviewed by a human — the heuristic cannot determine whether a
sensory reference is supplemented by other cues (which would be acceptable).

Patterns are grouped by sensory characteristic type so the remediator can
prioritise which pages to review.
"""

from __future__ import annotations

import re

# ── Pattern definitions ────────────────────────────────────────────────────────
# Each entry: (type_label, compiled_regex)
# Patterns are matched case-insensitively against struct element text.
# We require the match to be within a reasonable-length string (< 400 chars)
# to avoid flagging in dense narrative paragraphs where the phrase is incidental.

_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Visual location
    ("visual_location", re.compile(
        r"\b(see|click|refer to|as shown in|shown (on|in)|illustrated (on|in)|"
        r"displayed (on|in)|found (on|in)|located (on|in))\b.{0,60}"
        r"\b(left|right|top|bottom|above|below|upper|lower|corner|side|column|row|"
        r"opposite|adjacent|next to|beside|following|preceding|previous)\b",
        re.I,
    )),
    # Shape-only
    ("shape_only", re.compile(
        r"\b(click|press|select|tap|use|see|choose)\b.{0,40}"
        r"\b(circle|square|triangle|rectangle|oval|diamond|star|arrow|box|button|"
        r"round|rounded|pointed|chevron)\b",
        re.I,
    )),
    # Color-only action (different from 1.4.1 — this is about instructions)
    ("color_only_instruction", re.compile(
        r"\b(click|press|select|tap|use|choose|see|find|highlighted?|marked?|"
        r"shown?|displayed?|indicated?)\b.{0,50}"
        r"\b(red|green|blue|yellow|orange|purple|pink|grey|gray|black|white|"
        r"gold|silver|colou?red?|colou?r-coded?|highlighted?|shaded?)\b",
        re.I,
    )),
    # Size-only
    ("size_only", re.compile(
        r"\b(larger?|smaller?|biggest?|smallest?|wider?|narrower?|taller?|"
        r"shorter?|bigger?|tiny|large|small|bold(er)?|thick(er)?)\b.{0,40}"
        r"\b(button|icon|link|box|section|column|row|item|element|image|figure)\b",
        re.I,
    )),
    # Diagram/figure reference without cross-reference tag
    ("figure_reference_without_tag", re.compile(
        r"\b(see|refer to|as (shown|depicted|illustrated|described) in|"
        r"shown in|illustrated in|displayed in|per|according to)\b.{0,40}"
        r"\b(figure|fig\.?|diagram|chart|graph|image|illustration|picture|"
        r"table|exhibit|appendix)\b.{0,20}\d",
        re.I,
    )),
    # Orientation/position only
    ("orientation_only", re.compile(
        r"\b(rotate|flip|turn|landscape|portrait|horizontal|vertical|"
        r"upright|upside.?down|sideways)\b",
        re.I,
    )),
    # Sound-only (rare in PDFs but included for completeness)
    ("sound_only", re.compile(
        r"\b(you (will |can )?(hear|listen)|when you hear|listen for|"
        r"the (sound|beep|tone|chime|alert))\b",
        re.I,
    )),
]

# Minimum text length — very short spans (single words, labels) are skipped
MIN_TEXT_LEN = 20
# Cap on text length — don't scan novel-length strings
MAX_TEXT_LEN = 400


def _collect_text_nodes(obj, page_map: dict, _depth: int = 0) -> list[dict]:
    """Walk struct tree and return text-bearing leaf nodes."""
    if _depth > 80:
        return []
    results: list[dict] = []
    try:
        tag = str(obj.get("/S", ""))
    except Exception:
        return results

    # Collect text from /Alt or /ActualText on any element
    text_sources: list[str] = []
    for key in ("/Alt", "/ActualText"):
        try:
            val = obj.get(key)
            if val is not None:
                text_sources.append(str(val))
        except Exception:
            pass

    # Extract page number
    page_num = None
    try:
        pg = obj.get("/Pg")
        if pg is not None:
            page_num = page_map.get(id(pg))
    except Exception:
        pass

    for text in text_sources:
        if MIN_TEXT_LEN <= len(text) <= MAX_TEXT_LEN:
            results.append({"text": text, "tag": tag, "page": page_num})

    # Recurse into /K
    try:
        kids = obj.get("/K")
        if kids is None:
            return results
        import pikepdf as _pk
        if isinstance(kids, _pk.Array):
            for kid in kids:
                try:
                    if isinstance(kid, (_pk.Dictionary, _pk.Object)):
                        results.extend(_collect_text_nodes(kid, page_map, _depth + 1))
                except Exception:
                    continue
        elif isinstance(kids, (_pk.Dictionary, _pk.Object)):
            results.extend(_collect_text_nodes(kids, page_map, _depth + 1))
    except Exception:
        pass

    return results


def check_sensory(pdf_path: str) -> list[dict]:
    """Return advisory warnings for sensory-only reference patterns.

    Each warning dict has:
        type        — sensory characteristic category
        page        — 1-based page number (or None)
        text        — excerpt of the flagged text (first 120 chars)
        match       — the specific matched phrase
        description — human-readable explanation
    """
    try:
        import pikepdf
    except ImportError:
        return []

    try:
        pdf = pikepdf.open(pdf_path)
    except Exception:
        return []

    page_map: dict[int, int] = {}
    try:
        for i, page in enumerate(pdf.pages):
            page_map[id(page.obj)] = i + 1
    except Exception:
        pass

    text_nodes: list[dict] = []
    try:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is not None:
            text_nodes = _collect_text_nodes(struct_root, page_map)
    except Exception:
        pass
    finally:
        pdf.close()

    if not text_nodes:
        return []

    warnings: list[dict] = []
    seen: set[tuple] = set()  # deduplicate (page, match) pairs

    for node in text_nodes:
        text = node["text"]
        page = node["page"]
        for type_label, pattern in _PATTERNS:
            m = pattern.search(text)
            if not m:
                continue
            match_text = m.group(0).strip()
            dedup_key = (page, match_text[:60].lower())
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            type_descriptions = {
                "visual_location": (
                    "Instruction references visual location only (WCAG 1.3.3). "
                    "Ensure the referenced element is also identified by name, "
                    "heading, or other non-positional cue."
                ),
                "shape_only": (
                    "Instruction references shape only (WCAG 1.3.3). "
                    "Add a text label or accessible name alongside the shape reference."
                ),
                "color_only_instruction": (
                    "Instruction references color only (WCAG 1.3.3 + 1.4.1). "
                    "Supplement with a text label, icon, or pattern."
                ),
                "size_only": (
                    "Instruction references size only (WCAG 1.3.3). "
                    "Add a text label or other non-size identifier."
                ),
                "figure_reference_without_tag": (
                    "Figure/table cross-reference may rely on visual position (WCAG 1.3.3). "
                    "Ensure the referenced figure also has a programmatic cross-reference "
                    "or is identified by both number and descriptive title."
                ),
                "orientation_only": (
                    "Instruction references orientation only (WCAG 1.3.3). "
                    "Supplement with a text description."
                ),
                "sound_only": (
                    "Instruction references sound only (WCAG 1.3.3). "
                    "Provide a visual or text equivalent."
                ),
            }

            warnings.append({
                "type": type_label,
                "page": page,
                "text": text[:120],
                "match": match_text[:80],
                "description": type_descriptions.get(type_label, "Sensory-only reference detected (WCAG 1.3.3)."),
            })

    return warnings
