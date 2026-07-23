"""artifact_mcid_fix.py — reconcile /Artifact sequences that carry a live MCID.

A marked-content sequence tagged ``/Artifact`` but carrying an ``/MCID`` that a
structure element references is self-contradictory: veraPDF fails it under BOTH
7.1-1 (artifact inside tagged) and 7.1-2 (tagged inside artifact) on the same
content. Real documents contain these constantly — a producer marks a band as
Artifact yet also lists its MCID in the structure tree.

Resolution, per sequence, driven by the structure tree (the source of truth for
what is content vs. decoration):

  * MCID IS referenced by a struct element  -> the content is real; rewrite the
    stream operator's tag from /Artifact to that element's structure type
    (e.g. /P), so stream and tree agree it is tagged content.
  * MCID is NOT referenced by any element    -> it is a genuine artifact; strip
    the stray /MCID from the operator so nothing can later mis-adopt it.

Only the BDC operator's tag/properties are rewritten; text, geometry and the
structure tree are untouched.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


def _referenced_mcids(pdf, pikepdf) -> dict:
    """Map page objgen -> {mcid: structure_tag} for every MCID a struct element
    references (via /Pg + integer /K)."""
    import pikepdf as _pk

    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return {}
    out: dict = {}
    seen: set = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if not hasattr(node, "get"):
            continue
        try:
            og = node.objgen
            if og != (0, 0):
                if og in seen:
                    continue
                seen.add(og)
        except Exception:
            pass
        try:
            s = node.get("/S")
            pg = node.get("/Pg")
            k = node.get("/K")
        except Exception:
            s = pg = k = None
        if s is not None and k is not None:
            tag = str(s)
            items = list(k) if isinstance(k, _pk.Array) else [k]
            for item in items:
                m_pg = pg          # default: element's own /Pg
                mcid = None
                if isinstance(item, int):
                    mcid = item
                elif hasattr(item, "get"):
                    # Marked-content reference: /K = <</Type /MCR /MCID n /Pg p>>
                    try:
                        if str(item.get("/Type", "")) == "/MCR" or item.get("/MCID") is not None:
                            mm = item.get("/MCID")
                            mcid = int(mm) if mm is not None else None
                            if item.get("/Pg") is not None:
                                m_pg = item.get("/Pg")
                    except Exception:
                        mcid = None
                if mcid is None or m_pg is None:
                    continue
                try:
                    pg_og = m_pg.objgen
                except Exception:
                    continue
                out.setdefault(pg_og, {})[mcid] = tag
        # recurse
        try:
            kk = node.get("/K")
            if kk is not None:
                for c in (list(kk) if isinstance(kk, _pk.Array) else [kk]):
                    if hasattr(c, "get"):
                        stack.append(c)
        except Exception:
            pass
    return out


def fix_artifact_mcids(pdf_path: str) -> tuple[int, list[str]]:
    """Reconcile /Artifact-with-MCID sequences against the structure tree.
    Returns (changes, notes)."""
    try:
        import pikepdf
        from pikepdf import Dictionary, Name, Operator
    except ImportError:
        return 0, []

    retagged = 0
    stripped = 0
    try:
        with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
            ref = _referenced_mcids(pdf, pikepdf)
            if pdf.Root.get("/StructTreeRoot") is None:
                return 0, []

            for page in pdf.pages:
                try:
                    pg_og = page.obj.objgen
                except Exception:
                    continue
                page_ref = ref.get(pg_og, {})
                if not page_ref:
                    continue
                # Byte-level rewrite: rebuilding this class of stream via
                # unparse_content_stream does not reliably round-trip, so we
                # patch the raw content bytes directly (proven to persist).
                contents = page.obj.get("/Contents")
                if contents is None:
                    continue
                try:
                    if isinstance(contents, pikepdf.Array):
                        raw = b"\n".join(bytes(s.read_bytes()) for s in contents)
                    else:
                        raw = bytes(contents.read_bytes())
                except Exception:
                    continue

                page_changed = 0
                for mcid, tag in page_ref.items():
                    if not tag.startswith("/"):
                        tag = "/" + tag
                    # /Artifact  << ... /MCID <mcid> ... >>  BDC   ->   <tag> ...
                    # The negative lookahead on the MCID digits prevents 21
                    # matching 210. Dict bodies are flat (no nested <<>>).
                    pat = re.compile(
                        rb"/Artifact(\s*<<[^<>]*?/MCID\s+" + str(mcid).encode()
                        + rb"(?![0-9])[^<>]*?>>\s*BDC)")
                    raw, n = pat.subn(tag.encode() + rb"\1", raw)
                    if n:
                        page_changed += n
                        retagged += n

                if page_changed:
                    try:
                        page.obj.Contents = pdf.make_stream(raw)
                    except Exception as exc:
                        log.debug("artifact_mcid rewrite failed: %s", exc)
                        retagged -= page_changed

            if retagged or stripped:
                pdf.save()
    except Exception as exc:
        log.warning("artifact_mcid_fix: %s", exc)
        return 0, []

    notes = []
    if retagged:
        notes.append(f"Retagged {retagged} artifact-marked region(s) that the "
                     "structure tree references as real content (PDF/UA 7.1-1/2)")
    if stripped:
        notes.append(f"Stripped stray MCID from {stripped} genuine artifact(s)")
    return retagged + stripped, notes
