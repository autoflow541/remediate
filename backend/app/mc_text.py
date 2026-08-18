"""Shared marked-content text extraction.

A PDF structure element references its visible text only indirectly, by MCID:
the words live in the page content stream inside a `/Tag <</MCID n>> BDC … EMC`
sequence. To show a human *what* an element is — a heading's text in the audit
report, an element preview in the reading-order editor — we have to walk the
content stream and map each MCID back to the text it wraps.

Two helpers:

    page_mcid_texts(page) -> {mcid: text}
        Parse one page's content stream once; return the text of every MCID.

    element_mcids(elem) -> [int, ...]
        The MCIDs a struct element references directly (its /K, whether a bare
        integer or an array of integers). Nested child struct elements and OBJR
        references are ignored — the caller recurses if it wants their text.
"""

from __future__ import annotations


def _op_text(operand) -> str:
    """Decode a content-stream string operand to text (best effort)."""
    try:
        return str(operand)
    except Exception:
        return ""


def page_mcid_texts(page) -> dict[int, str]:
    """Map MCID -> concatenated visible text for one page's content stream.

    Handles nested marked content (BDC/BMC … EMC) via a stack, and the four
    text-showing operators (Tj, TJ, ', "). Returns {} on any parse failure so
    callers can treat "no text" and "unparseable" identically.
    """
    import pikepdf

    chunks: dict[int, list[str]] = {}
    stack: list[int | None] = []
    current: int | None = None

    try:
        for instr in pikepdf.parse_content_stream(page):
            op = str(instr.operator)
            operands = instr.operands

            if op in ("BDC", "BMC"):
                mcid: int | None = None
                if op == "BDC" and len(operands) >= 2:
                    props = operands[1]
                    try:
                        m = props.get("/MCID") if hasattr(props, "get") else None
                        mcid = int(m) if m is not None else None
                    except Exception:
                        mcid = None
                stack.append(current)
                # A nested sequence without its own MCID inherits the enclosing one.
                current = mcid if mcid is not None else current
            elif op == "EMC":
                current = stack.pop() if stack else None
            elif current is not None and op in ("Tj", "'", '"'):
                # ' and " show a string as their last operand (after an implicit
                # line move / spacing operands, which don't affect the text).
                if operands:
                    chunks.setdefault(current, []).append(_op_text(operands[-1]))
            elif current is not None and op == "TJ" and operands:
                try:
                    parts = [
                        _op_text(x) for x in operands[0]
                        if isinstance(x, pikepdf.String)
                    ]
                    chunks.setdefault(current, []).append("".join(parts))
                except Exception:
                    pass
    except Exception:
        return {}

    return {mcid: "".join(parts).strip() for mcid, parts in chunks.items()}


def element_mcids(elem) -> list[int]:
    """Return the MCIDs a struct element references directly via its /K.

    /K may be a bare integer (one MCID), an array mixing integers with child
    struct dicts / OBJR dicts, or a dict (a child element, no direct MCID).
    """
    import pikepdf

    try:
        k = elem.get("/K")
    except Exception:
        return []
    if k is None:
        return []
    if isinstance(k, int):
        return [k]
    if isinstance(k, pikepdf.Array):
        out: list[int] = []
        for item in k:
            if isinstance(item, int):
                out.append(int(item))
        return out
    return []
