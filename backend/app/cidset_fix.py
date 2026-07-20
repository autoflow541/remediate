"""cidset_fix.py — veraPDF 7.21.4.2-2: incomplete /CIDSet in embedded CID fonts.

PDF/UA-1 (via ISO 19005 heritage): *if* a FontDescriptor of an embedded CID
font contains a /CIDSet stream, it must identify every CID used in the file.
Producers routinely emit incomplete CIDSets, which is unfixable without
re-subsetting the font — but the key itself is OPTIONAL. Removing it satisfies
the clause with zero rendering impact (CIDSet is metadata about the subset,
not used for drawing).

Benchmark data: 3 of 6 repair-mode documents failed this clause.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def remove_incomplete_cidsets(pdf_path: str) -> tuple[int, list[str]]:
    """Delete /CIDSet from every CID font descriptor. In-place; returns
    (count_removed, notes)."""
    try:
        import pikepdf
        from pikepdf import Name
    except ImportError:
        return 0, []

    removed = 0
    try:
        with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
            seen: set = set()
            for page in pdf.pages:
                res = page.get("/Resources")
                fonts = res.get("/Font") if res else None
                if not fonts:
                    continue
                for _, font_ref in fonts.items():
                    try:
                        font = font_ref
                        # Type0 fonts keep the real descriptor on the descendant.
                        desc_fonts = font.get("/DescendantFonts")
                        candidates = list(desc_fonts) if desc_fonts is not None else [font]
                        for cand in candidates:
                            fd = cand.get("/FontDescriptor")
                            if fd is None:
                                continue
                            key = getattr(fd, "objgen", None)
                            if key in seen:
                                continue
                            if key is not None:
                                seen.add(key)
                            if fd.get("/CIDSet") is not None:
                                del fd[Name("/CIDSet")]
                                removed += 1
                    except Exception:
                        continue
            if removed:
                pdf.save()
    except Exception as exc:
        log.warning("cidset_fix: %s", exc)
        return 0, []

    notes = ([f"Removed {removed} incomplete /CIDSet entr{'ies' if removed != 1 else 'y'} "
              "(optional key; incomplete sets fail veraPDF 7.21.4.2)"] if removed else [])
    return removed, notes
