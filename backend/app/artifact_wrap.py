"""artifact_wrap.py — mark stray unmarked content as /Artifact (PDF/UA 7.1-3).

Repair-mode documents keep their original structure tree, but many real-world
tagged PDFs contain painting operators bound to no tag at all — decorative
rules, backgrounds, stray text runs the producer never marked. veraPDF 7.1-3
("content shall be marked as Artifact or tagged as real content") fails once
per such operator; the benchmark's worst repair-mode documents carried
thousands of these checks.

The rebuild path never has this problem (writeback re-marks everything). This
module gives the repair path the same guarantee without touching the existing
tree: every painting operator that sits at marked-content depth 0 is wrapped
individually in ``BMC /Artifact … EMC``. Content already inside a BDC/BMC
group is untouched, so existing tags and their MCIDs are preserved exactly.

Wrapping each operator individually (rather than runs) keeps the nesting
trivially legal with respect to BT/ET and q/Q, at a small size cost.

Conservative by design: page content streams only (Form XObjects are left
alone), and any page whose stream fails to parse is skipped.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Operators that render something (text, paths, images, shading).
_PAINT_OPS = {
    "Tj", "TJ", "'", '"',                            # text
    "S", "s", "f", "F", "f*", "B", "B*", "b", "b*",  # path painting
    "Do", "sh", "BI", "EI", "INLINE IMAGE",          # xobjects / shading / inline img
}


def wrap_unmarked_content(pdf_path: str) -> tuple[int, list[str]]:
    """Wrap unmarked painting operators in /Artifact groups, in place.

    Returns (operators_wrapped, notes).
    """
    try:
        import pikepdf
        from pikepdf import Name, Operator
    except ImportError:
        return 0, []

    total = 0
    try:
        with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
            # Only meaningful for tagged documents — untagged ones get the
            # rebuild path, which marks everything itself.
            if pdf.Root.get("/StructTreeRoot") is None:
                return 0, []

            for page in pdf.pages:
                try:
                    instructions = list(pikepdf.parse_content_stream(page))
                except Exception:
                    continue

                depth = 0
                wrapped = 0
                out: list = []
                bmc = pikepdf.ContentStreamInstruction([Name.Artifact], Operator("BMC"))
                emc = pikepdf.ContentStreamInstruction([], Operator("EMC"))

                for instr in instructions:
                    op = str(getattr(instr, "operator", ""))
                    if op in ("BDC", "BMC"):
                        depth += 1
                        out.append(instr)
                        continue
                    if op == "EMC":
                        depth = max(0, depth - 1)
                        out.append(instr)
                        continue
                    if depth == 0 and (op in _PAINT_OPS or isinstance(
                            instr, getattr(pikepdf, "ContentStreamInlineImage", ()))):
                        out.append(bmc)
                        out.append(instr)
                        out.append(emc)
                        wrapped += 1
                        continue
                    out.append(instr)

                if wrapped:
                    try:
                        page.obj.Contents = pdf.make_stream(
                            pikepdf.unparse_content_stream(out))
                        total += wrapped
                    except Exception as exc:
                        log.debug("artifact_wrap page rewrite failed: %s", exc)

            if total:
                pdf.save()
    except Exception as exc:
        log.warning("artifact_wrap: %s", exc)
        return 0, []

    notes = ([f"Wrapped {total} unmarked content operator{'s' if total != 1 else ''} "
              "as /Artifact (PDF/UA 7.1-3)"] if total else [])
    return total, notes
