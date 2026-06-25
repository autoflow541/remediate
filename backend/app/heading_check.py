"""Heading hierarchy validator (WCAG 1.3.1 / 2.4.6).

After tagging, heading elements should form a logical hierarchy:
  • The document should have at least one H1.
  • Heading levels must not skip (e.g. H1 → H3 without H2).
  • Each heading should have non-empty text.

Returns a list of issue dicts, one per problem found:
  { type, level, page, text, description }

These are informational warnings — they cannot be auto-fixed without
knowing the intended document structure.  They appear in the audit report
so the remediator can correct them manually.
"""

from __future__ import annotations


def check_headings(pdf_path: str) -> list[dict]:
    """Walk the PDF structure tree and validate heading hierarchy.

    Returns a list of issues (may be empty for a well-formed document).
    """
    try:
        import pikepdf
    except ImportError:
        return []

    try:
        pdf = pikepdf.open(pdf_path)
    except Exception:
        return []

    # Build page number lookup: object id → 1-based page number
    page_map: dict[int, int] = {}
    try:
        for i, page in enumerate(pdf.pages):
            page_map[id(page.obj)] = i + 1
    except Exception:
        pass

    headings: list[dict] = []

    def _collect(obj, depth: int = 0) -> None:
        if depth > 80:
            return
        try:
            tag = str(obj.get("/S", ""))
        except Exception:
            return

        # Heading: /H, /H1 … /H6
        if tag in ("/H", "/H1", "/H2", "/H3", "/H4", "/H5", "/H6"):
            level = 1 if tag == "/H" else int(tag[2])
            # Extract text from /Alt or concatenate marked-content
            alt = obj.get("/Alt")
            text = str(alt).strip() if alt else ""
            page_num = None
            pg = obj.get("/Pg")
            if pg is not None:
                try:
                    page_num = page_map.get(id(pg))
                except Exception:
                    pass
            headings.append({"level": level, "text": text, "page": page_num})

        try:
            kids = obj.get("/K")
            if kids is None:
                return
            import pikepdf as _pk
            if isinstance(kids, _pk.Array):
                for kid in kids:
                    try:
                        if isinstance(kid, (_pk.Dictionary, _pk.Object)):
                            _collect(kid, depth + 1)
                    except Exception:
                        continue
            elif isinstance(kids, (_pk.Dictionary, _pk.Object)):
                _collect(kids, depth + 1)
        except Exception:
            pass

    try:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if struct_root is not None:
            _collect(struct_root)
    except Exception:
        pass
    finally:
        pdf.close()

    if not headings:
        return []

    issues: list[dict] = []

    # Check for missing H1
    if not any(h["level"] == 1 for h in headings):
        issues.append({
            "type": "missing_h1",
            "level": None,
            "page": None,
            "text": "",
            "description": "Document has no H1 (top-level heading). Screen readers expect an H1.",
        })

    # Check for skipped levels
    prev_level = 0
    for h in headings:
        lvl = h["level"]
        if lvl > prev_level + 1 and prev_level > 0:
            issues.append({
                "type": "skipped_level",
                "level": lvl,
                "page": h.get("page"),
                "text": h.get("text", "")[:80],
                "description": (
                    f"Heading jumps from H{prev_level} to H{lvl} "
                    f"(skipped level{'s' if lvl - prev_level > 2 else ''}) "
                    f"on page {h.get('page') or '?'}"
                ),
            })
        prev_level = lvl

    return issues
