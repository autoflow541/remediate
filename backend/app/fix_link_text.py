"""Auto-generate descriptive accessible names for non-descriptive PDF links.

WCAG 2.4.4 (Link Purpose) requires that a link's purpose can be determined
from the link text alone *or* from the link text combined with its accessible
name (/Alt on the Link structure element).

When a link says "Click here" or "Read more", we cannot change the visible
text in the PDF content stream without re-rendering the page.  Instead we
write a descriptive /Alt attribute on the Link structure element in the
tagged PDF.  Screen readers announce /Alt (if present) in preference to the
visible text, so the fix is fully accessible even though the visual appearance
is unchanged.

Description strategy (in priority order):
  1. URL slug  – clean up the last path segment into readable English
  2. Claude Haiku – if the slug is ambiguous, ask Claude with URL + context
  3. Domain fallback – "Resource on <domain>"
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

# Acronyms to upper-case regardless of slug casing
_ACRONYMS = frozenset(["wcag", "pdf", "ua", "aria", "html", "ada", "508", "atag", "uaag"])

# Slug words that are generic and need more context (same list as link_quality._GENERIC)
_GENERIC_SLUGS = frozenset([
    "index", "home", "page", "link", "here", "more", "info",
    "download", "view", "open", "get", "go", "visit",
])


def _slug_to_label(path: str) -> str | None:
    """Convert a URL path into a human-readable label, or return None if too generic."""
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return None

    slug = parts[-1]
    # Strip file extension (.html, .pdf, .aspx, …)
    slug = re.sub(r"\.\w{2,5}$", "", slug)
    slug = slug.replace("-", " ").replace("_", " ").strip()

    if not slug or slug.lower() in _GENERIC_SLUGS:
        # Try the parent path segment
        if len(parts) >= 2:
            slug = parts[-2].replace("-", " ").replace("_", " ").strip()
        if not slug or slug.lower() in _GENERIC_SLUGS:
            return None

    # Upper-case known acronyms
    for acronym in _ACRONYMS:
        slug = re.sub(rf"(?i)\b{re.escape(acronym)}\b", acronym.upper(), slug)

    return slug.title()


def _ask_claude(url: str, context: str) -> str | None:
    """Ask Claude Haiku to generate a short link label (max 60 chars).

    Returns None if the Anthropic client is unavailable or the call fails.
    """
    try:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        client = anthropic.Anthropic(api_key=api_key)
        prompt = (
            f"Generate a concise, descriptive label (5 words or fewer) for a hyperlink.\n"
            f"URL: {url}\n"
            f"Surrounding document text: {context[:300]}\n\n"
            f"Return only the label text, no punctuation, no explanation."
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=40,
            messages=[{"role": "user", "content": prompt}],
        )
        label = msg.content[0].text.strip().strip('"').strip("'")
        return label[:80] if label else None
    except Exception:
        return None


def generate_link_description(url: str, context: str = "") -> str:
    """Return a human-readable accessible name for a hyperlink URL.

    Falls back through: slug → Claude → domain.
    """
    if not url:
        return "Link"

    try:
        parsed = urlparse(url)
    except Exception:
        return "Link"

    domain = parsed.netloc.replace("www.", "") or url[:40]

    # 1. Try URL slug
    label = _slug_to_label(parsed.path)
    if label:
        return f"{label} — {domain}"

    # 2. Try Claude (needs ANTHROPIC_API_KEY)
    if context or domain:
        label = _ask_claude(url, context)
        if label:
            return label

    # 3. Domain fallback
    return f"Resource on {domain}"


def fix_link_accessible_names(
    pdf_path: str, issues: list[dict]
) -> tuple[list[dict], list[dict], int]:
    """Update /Alt on Link structure elements for non-descriptive links.

    For each issue in *issues* (from check_link_quality), generate a
    descriptive label and patch the corresponding Link struct element in the
    tagged PDF.  Issues that were successfully patched are moved to the
    returned *fixed* list; the remainder stay in *remaining*.

    Returns (remaining_issues, fixed_issues, patch_count).
    """
    if not issues:
        return issues, [], 0

    try:
        import pikepdf
    except ImportError:
        return issues, [], 0

    try:
        pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)
    except Exception:
        return issues, [], 0

    # Build a lookup: url → generated description
    descriptions: dict[str, str] = {}
    for issue in issues:
        url = issue.get("url", "")
        if url and url not in descriptions:
            # Use any surrounding text extracted during quality check as context
            context = issue.get("text", "")
            descriptions[url] = generate_link_description(url, context)

    patched_urls: set[str] = set()

    def _walk_struct(obj):
        """Depth-first walk of the PDF structure tree."""
        try:
            tag = str(obj.get("/S", ""))
        except Exception:
            return

        if tag == "/Link":
            try:
                # Determine which URL this Link element references
                annot_refs = obj.get("/K")
                if annot_refs is not None:
                    # /K may point to an OBJR (object reference) for the annotation
                    refs = annot_refs if isinstance(annot_refs, pikepdf.Array) else [annot_refs]
                    for ref in refs:
                        try:
                            ref_obj = ref
                            if str(ref_obj.get("/Type", "")) == "/OBJR":
                                annot = ref_obj.get("/Obj")
                                if annot is not None:
                                    action = annot.get("/A")
                                    if action is not None:
                                        uri_obj = action.get("/URI")
                                        if uri_obj is not None:
                                            uri = str(uri_obj)
                                            if uri in descriptions:
                                                obj["/Alt"] = pikepdf.String(descriptions[uri])
                                                patched_urls.add(uri)
                        except Exception:
                            continue
            except Exception:
                pass

        # Recurse into children
        try:
            kids = obj.get("/K")
            if kids is None:
                return
            if isinstance(kids, pikepdf.Array):
                for kid in kids:
                    try:
                        _walk_struct(kid)
                    except Exception:
                        continue
            else:
                _walk_struct(kids)
        except Exception:
            pass

    try:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is not None:
            _walk_struct(struct_root)
        pdf.save(pdf_path)
    except Exception:
        pdf.close()
        return issues, [], 0

    pdf.close()

    # Split issues into fixed vs remaining
    remaining = []
    fixed = []
    for issue in issues:
        url = issue.get("url", "")
        if url in patched_urls:
            fixed.append({
                **issue,
                "autoFixedAlt": descriptions.get(url, ""),
            })
        else:
            remaining.append(issue)

    return remaining, fixed, len(patched_urls)
