"""RoleMap writer for custom structure types (Sprint 10 — PDF/UA-1 §7.1).

PDF/UA-1 clause 7.1 requires that any non-standard structure type used in the
struct tree is mapped to a standard PDF type via the StructTreeRoot/RoleMap
dictionary.  Without a RoleMap entry, veraPDF fails with clause 7.1-1.

Common non-standard tags encountered in practice:
  - 'Footnote'     → Note
  - 'Endnote'      → Note
  - 'Code'         → P
  - 'BlockQuote'   → BlockQuote  (is standard in PDF 1.7+; map anyway for 1.4)
  - 'Callout'      → Note
  - 'Sidebar'      → Note
  - 'TextBox'      → P
  - 'Abstract'     → P
  - 'Title'        → H1  (some generators emit 'Title' instead of H1)
  - 'Subtitle'     → H2
  - 'Author'       → P
  - 'Date'         → P
  - 'TOC'          → TOC  (already standard, included for safety)
  - 'TOCI'         → TOCI

Called from StructTreeBuilder.apply() after build_tree().
"""

from __future__ import annotations

import pikepdf
from pikepdf import Array, Dictionary, Name

# Maps non-standard tag → nearest PDF/UA standard tag
_ROLE_MAP: dict[str, str] = {
    "Footnote":   "Note",
    "Endnote":    "Note",
    "Note":       "Note",
    "Callout":    "Note",
    "Sidebar":    "Aside",
    "Aside":      "Aside",
    "TextBox":    "P",
    "Abstract":   "P",
    "Code":       "Code",
    "BlockQuote": "BlockQuote",
    "Title":      "H1",
    "Subtitle":   "H2",
    "Author":     "P",
    "Date":       "P",
    "Caption":    "Caption",
    "Lbl":        "Lbl",
    "LBody":      "LBody",
    "TOC":        "TOC",
    "TOCI":       "TOCI",
    "Formula":    "Formula",
}

# Standard PDF 1.7 / PDF/UA-1 structure types (no RoleMap entry needed)
_STANDARD_TAGS = {
    "Document", "Part", "Art", "Sect", "Div", "BlockQuote", "Caption",
    "TOC", "TOCI", "Index", "NonStruct", "Private",
    "H", "H1", "H2", "H3", "H4", "H5", "H6",
    "P", "L", "LI", "Lbl", "LBody",
    "Table", "TR", "TH", "TD", "THead", "TBody", "TFoot",
    "Span", "Quote", "Note", "Reference", "BibEntry", "Code",
    "Link", "Annot", "Ruby", "Warichu",
    "Figure", "Formula", "Form",
    "Aside", "Title", "FENote",  # PDF 2.0 / PDF/UA-2
    "Artifact",
}


def _collect_tags(nodes: list[dict], found: set[str]) -> None:
    for n in nodes:
        tag = n.get("tag", "P")
        if tag:
            found.add(tag)
        _collect_tags(n.get("children") or [], found)


def write_role_map(pdf: pikepdf.Pdf, manifest: dict, struct_root) -> int:
    """Add /RoleMap entries for non-standard tags found in the manifest.

    Returns the count of RoleMap entries written.
    """
    found_tags: set[str] = set()
    _collect_tags(manifest.get("nodes") or [], found_tags)

    entries: dict[str, str] = {}
    for tag in found_tags:
        if tag in _STANDARD_TAGS:
            continue  # no mapping needed
        mapped = _ROLE_MAP.get(tag)
        if mapped:
            entries[tag] = mapped
        else:
            # Unknown custom tag — map to NonStruct as a safe default
            entries[tag] = "NonStruct"

    if not entries:
        return 0

    role_map = Dictionary()
    for src, dst in entries.items():
        role_map[f"/{src}"] = Name(f"/{dst}")

    struct_root.RoleMap = role_map
    return len(entries)
