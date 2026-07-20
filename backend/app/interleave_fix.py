"""interleave_fix.py — untangle artifact/tagged marked-content nesting.

PDF/UA 7.1-1 / 7.1-2: artifact content must not sit inside tagged content and
vice versa. Real-world producers violate this constantly (an /Artifact BMC
spanning a whole page band that also contains tagged BDC regions).

Two mechanical, content-preserving rewrites:

  * tagged BDC opened while inside an /Artifact region -> the artifact region
    is SPLIT: closed before the tagged group opens, re-opened after it closes.
  * /Artifact BMC/BDC opened while inside a tagged region -> the artifact
    marks are DROPPED; the enclosed content inherits the surrounding tag
    (content an author placed inside a tagged region is announced with it).

Single pass with an explicit group stack; every other operator passes through
untouched, so MCIDs, text and geometry are preserved exactly.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _is_artifact_open(instr) -> bool:
    try:
        return str(instr.operands[0]) == "/Artifact"
    except Exception:
        return False


def fix_interleaved_marked_content(pdf_path: str) -> tuple[int, list[str]]:
    """Fix artifact/tagged interleavings in all page streams. Returns
    (rewrites, notes)."""
    try:
        import pikepdf
        from pikepdf import Name, Operator
    except ImportError:
        return 0, []

    total = 0
    try:
        with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
            if pdf.Root.get("/StructTreeRoot") is None:
                return 0, []

            bmc_artifact = pikepdf.ContentStreamInstruction([Name.Artifact], Operator("BMC"))
            emc = pikepdf.ContentStreamInstruction([], Operator("EMC"))

            for page in pdf.pages:
                try:
                    instructions = list(pikepdf.parse_content_stream(page))
                except Exception:
                    continue

                # stack entries: "artifact" | "artifact-dropped" | "tagged"
                stack: list[str] = []
                fixes = 0
                out: list = []

                for instr in instructions:
                    op = str(getattr(instr, "operator", ""))

                    if op in ("BMC", "BDC"):
                        artifact = _is_artifact_open(instr)
                        inside_tagged = "tagged" in stack
                        inside_artifact = stack.count("artifact") > 0

                        if artifact and inside_tagged:
                            # drop artifact marks inside tagged content
                            stack.append("artifact-dropped")
                            fixes += 1
                            continue
                        if not artifact and inside_artifact:
                            # split every open artifact region around this group
                            n_open = stack.count("artifact")
                            for _ in range(n_open):
                                out.append(emc)
                            stack = ["artifact-split" if s == "artifact" else s
                                     for s in stack]
                            stack.append("tagged")
                            out.append(instr)
                            fixes += 1
                            continue
                        stack.append("artifact" if artifact else "tagged")
                        out.append(instr)
                        continue

                    if op == "EMC":
                        kind = stack.pop() if stack else "tagged"
                        if kind == "artifact-dropped":
                            continue  # its BMC was dropped too
                        out.append(instr)
                        if kind == "tagged":
                            # re-open any artifact regions we split for this group
                            n_split = stack.count("artifact-split")
                            if n_split:
                                for _ in range(n_split):
                                    out.append(bmc_artifact)
                                stack = ["artifact" if s == "artifact-split" else s
                                         for s in stack]
                        continue

                    out.append(instr)

                if fixes:
                    try:
                        page.obj.Contents = pdf.make_stream(
                            pikepdf.unparse_content_stream(out))
                        total += fixes
                    except Exception as exc:
                        log.debug("interleave_fix rewrite failed: %s", exc)

            if total:
                pdf.save()
    except Exception as exc:
        log.warning("interleave_fix: %s", exc)
        return 0, []

    notes = ([f"Untangled {total} artifact/tagged marked-content interleaving"
              f"{'s' if total != 1 else ''} (PDF/UA 7.1-1/7.1-2)"] if total else [])
    return total, notes
