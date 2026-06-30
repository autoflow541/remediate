"""Alt text quality checker (WCAG 1.1.1 – Non-text Content).

After tagging, every Figure structure element should have a meaningful /Alt
attribute. This module walks the structure tree and flags elements with:
  • No /Alt at all
  • Empty /Alt (empty string or whitespace only)
  • Generic /Alt — common placeholder phrases that convey no information
    (e.g., "image", "photo", "figure 1", "graphic", "logo", "icon")

Returns a list of issues for the remediation report. These cannot be
auto-fixed without understanding the image content; they are surfaced as
"needs human review" warnings in the UI.
"""

from __future__ import annotations

import re

# Generic alt-text phrases that fail WCAG 1.1.1.
# Matched after stripping punctuation and lower-casing.
_GENERIC_EXACT = frozenset([
    "image", "photo", "photograph", "picture", "graphic", "graphics",
    "logo", "icon", "figure", "chart", "graph", "diagram", "illustration",
    "thumbnail", "banner", "header", "footer", "background", "spacer",
    "pixel", "placeholder", "untitled", "no description", "no alt",
    "alt text", "image description", "insert image", "img", "png", "jpg",
    "jpeg", "gif", "svg",
])

_GENERIC_PATTERNS = [
    re.compile(r"^figure\s*\d+\.?$", re.I),
    re.compile(r"^image\s*\d+\.?$", re.I),
    re.compile(r"^photo\s*\d+\.?$", re.I),
    re.compile(r"^slide\s*\d+\.?$", re.I),
    re.compile(r"^page\s*\d+\.?$", re.I),
    re.compile(r"^img[\-_]\d+$", re.I),
    re.compile(r"^\w{1,3}\d{3,}$"),  # e.g. "img001", "p12345" — filename fragments
]


def _is_generic(alt: str) -> bool:
    cleaned = re.sub(r"[^\w\s]", "", alt).strip().lower()
    if not cleaned:
        return True
    if cleaned in _GENERIC_EXACT:
        return True
    for pat in _GENERIC_PATTERNS:
        if pat.match(cleaned):
            return True
    return False


def _check_struct_node(obj, page_map: dict, issues: list[dict], _depth: int = 0) -> None:
    """Recursively walk structure tree, flag Figure nodes with bad alt."""
    if _depth > 60:  # guard against pathological trees
        return
    try:
        tag = str(obj.get("/S", ""))
    except Exception:
        return

    if tag == "/Figure":
        alt_obj = obj.get("/Alt")
        page_num = None
        # Try to infer page from /Pg on the element or its children
        pg_ref = obj.get("/Pg")
        if pg_ref is not None:
            try:
                page_num = page_map.get(id(pg_ref))
            except Exception:
                pass

        issue: dict | None = None

        if alt_obj is None:
            issue = {
                "type": "missing",
                "alt": None,
                "page": page_num,
                "description": "Figure has no alt text (/Alt attribute missing)",
            }
        else:
            alt_str = str(alt_obj)
            if not alt_str.strip():
                issue = {
                    "type": "empty",
                    "alt": "",
                    "page": page_num,
                    "description": "Figure has empty alt text",
                }
            elif _is_generic(alt_str):
                issue = {
                    "type": "generic",
                    "alt": alt_str[:120],
                    "page": page_num,
                    "description": f'Figure alt text is non-descriptive: "{alt_str[:60]}"',
                }

        if issue is not None:
            issues.append(issue)

    # Recurse into /K children
    try:
        kids = obj.get("/K")
        if kids is None:
            return
        import pikepdf
        if isinstance(kids, pikepdf.Array):
            for kid in kids:
                try:
                    if isinstance(kid, (pikepdf.Dictionary, pikepdf.Object)):
                        _check_struct_node(kid, page_map, issues, _depth + 1)
                except Exception:
                    continue
        elif isinstance(kids, (pikepdf.Dictionary, pikepdf.Object)):
            _check_struct_node(kids, page_map, issues, _depth + 1)
    except Exception:
        pass


def check_alt_quality(pdf_path: str) -> list[dict]:
    """Return a list of Figure alt-text issues in the tagged PDF.

    Each issue has: type (missing|empty|generic), alt, page, description.
    """
    try:
        import pikepdf
    except ImportError:
        return []

    try:
        pdf = pikepdf.open(pdf_path)
    except Exception:
        return []

    # Build page_num lookup: object id → 1-based page number
    page_map: dict[int, int] = {}
    try:
        for i, page in enumerate(pdf.pages):
            page_map[id(page.obj)] = i + 1
    except Exception:
        pass

    issues: list[dict] = []
    try:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is not None:
            _check_struct_node(struct_root, page_map, issues)
    except Exception:
        pass
    finally:
        pdf.close()

    return issues
