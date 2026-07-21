"""ai_visual_fix.py — automatically FIX what the AI visual review finds.

ai_visual_check.py flags the judgment items (headings that are visually
prominent but tagged P, wrong alt text, title mismatch). This module closes the
loop: one Claude vision call returns both findings AND concrete fix actions
from a safe dispatch table; the engine applies them to the structure tree,
repairs any heading-level skips that retagging introduced, and re-validates.

Safe-dispatch actions (no code generation, no free-form edits):
  retag      — change a struct element's /S (e.g. P -> H2). The element is
               located by its text; the fix is SKIPPED unless exactly one
               element matches (fail-safe against retagging the wrong block).
  set_alt    — replace a Figure's /Alt (figure addressed by document order).
  set_title  — set the document title (docinfo + XMP) to the visible title.

Anything not expressible in those actions (overlapping text, untagged lists,
genuinely ambiguous calls) is returned as a remaining finding for the human.

A regression guard validates before/after: if the fixes make veraPDF worse,
the original file is restored and the fixes are reported as skipped.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile

log = logging.getLogger(__name__)

# Sonnet-5 (no extended thinking) instead of Opus-4.8+adaptive: the visual
# review was ~80s/document, the dominant cost of /remediate. Sonnet is strong
# at this vision-grounded structural comparison, and every fix it proposes
# still passes through the validate-guard below (reverted if veraPDF regresses),
# so the speed/quality trade is safe. Override with AI_VISUAL_MODEL.
_MODEL = os.environ.get("AI_VISUAL_MODEL", "claude-sonnet-5")
_MAX_TOKENS = 8000
_ALLOWED_RETAGS = {"H1", "H2", "H3", "H4", "H5", "H6", "P", "Caption"}

_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["retag", "set_alt", "set_title"]},
                    "target_text": {
                        "type": "string",
                        "description": "retag only: the block's visible text, verbatim, enough to uniquely identify it.",
                    },
                    "new_tag": {
                        "type": "string",
                        "enum": sorted(_ALLOWED_RETAGS),
                        "description": "retag only: the structure tag the block should have.",
                    },
                    "figure_number": {
                        "type": "integer",
                        "description": "set_alt only: 1-based figure index in document order.",
                    },
                    "new_alt": {"type": "string", "description": "set_alt only: corrected alt text."},
                    "new_title": {"type": "string", "description": "set_title only: the visible document title."},
                    "reason": {"type": "string"},
                },
                "required": ["action", "target_text", "new_tag", "figure_number", "new_alt", "new_title", "reason"],
                "additionalProperties": False,
            },
        },
        "remaining": {
            "type": "array",
            "description": "Findings that need a human (not fixable via the actions).",
            "items": {
                "type": "object",
                "properties": {
                    "check": {
                        "type": "string",
                        "enum": ["alt_text", "reading_order", "headings", "decorative", "tables", "title", "other"],
                    },
                    "verdict": {"type": "string", "enum": ["needs_human", "likely_problem"]},
                    "page": {"type": "integer"},
                    "detail": {"type": "string"},
                },
                "required": ["check", "verdict", "page", "detail"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "fixes", "remaining"],
    "additionalProperties": False,
}

_INSTRUCTIONS = """You are reviewing a PDF that was just auto-remediated for accessibility \
(PDF/UA structure tags were written into it). You are given renders of its first pages and \
its structure tree (tags in reading order with each element's extracted text, plus figure \
alt texts, title, language).

Your job has two outputs:

1. `fixes` — concrete corrections the engine will apply mechanically. Available actions:
   - retag: a block whose visual role doesn't match its tag (a visually prominent section \
title tagged P should become H2/H3; a mis-tagged heading that is really body text should \
become P; a figure caption tagged P directly adjacent to a figure may become Caption). \
`target_text` must be the block's text VERBATIM as it appears in the structure tree \
element list (copy it exactly — matching is text-based and skips ambiguous matches). \
Choose heading levels so the visual hierarchy maps to H1 > H2 > H3 without skips.
   - set_alt: a figure whose alt text is clearly wrong, generic, or unhelpful given what \
the image visibly shows. Provide corrected alt (concise, describes content and purpose).
   - set_title: the document title in metadata doesn't match the visible title on page 1. \
Provide the visible title.
   Only propose a fix when you are CONFIDENT from the render. When unsure, put it in \
`remaining` instead.

2. `remaining` — real problems you can see but cannot fix with the actions above \
(reading-order jumps, untagged lists, overlapping/illegible text, ambiguous decorative \
choices), for the human reviewer. Include the page and specifics.

For unused fields in a fix, use an empty string ("") or 0. Only reference pages you were \
shown."""


# ---------------------------------------------------------------------------
# MCID -> text extraction (the writeback marks every run with BDC /Tag <</MCID n>>)
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    """Normalize text for matching: lowercase, alnum+spaces collapsed."""
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _page_mcid_texts(pdf, page) -> dict[int, str]:
    """Map MCID -> concatenated text for one page's content stream."""
    import pikepdf

    texts: dict[int, list[str]] = {}
    current: int | None = None
    try:
        for instr in pikepdf.parse_content_stream(page):
            op = str(instr.operator)
            operands = instr.operands
            if op == "BDC" and len(operands) == 2:
                try:
                    props = operands[1]
                    mcid = props.get("/MCID") if hasattr(props, "get") else None
                    current = int(mcid) if mcid is not None else None
                except Exception:
                    current = None
            elif op in ("EMC", "BMC"):
                if op == "EMC":
                    current = None
            elif current is not None and op in ("Tj", "'", '"'):
                try:
                    texts.setdefault(current, []).append(str(operands[-1]))
                except Exception:
                    pass
            elif current is not None and op == "TJ":
                try:
                    parts = [str(x) for x in operands[0] if isinstance(x, pikepdf.String)]
                    texts.setdefault(current, []).append("".join(parts))
                except Exception:
                    pass
    except Exception as exc:
        log.debug("visual_fix mcid texts: %s", exc)
    return {m: " ".join(t) for m, t in texts.items()}


def _collect_elements(pdf) -> list[dict]:
    """Flatten the structure tree to [{obj, tag, text, figure_no}] in order."""
    import pikepdf

    # Build page-object -> mcid text maps lazily.
    page_maps: dict[tuple, dict[int, str]] = {}
    page_objgens = {}
    for i, page in enumerate(pdf.pages):
        page_objgens[page.obj.objgen] = page

    def mcid_text(elem) -> str:
        try:
            pg = elem.get("/Pg")
            if pg is None:
                return ""
            og = pg.objgen
            if og not in page_maps:
                page = page_objgens.get(og)
                page_maps[og] = _page_mcid_texts(pdf, page) if page is not None else {}
            k = elem.get("/K")
            mcids = []
            if isinstance(k, int):
                mcids = [int(k)]
            elif isinstance(k, pikepdf.Array):
                mcids = [int(x) for x in k if isinstance(x, int)]
            else:
                try:
                    mcids = [int(k)]
                except Exception:
                    mcids = []
            return " ".join(page_maps[og].get(m, "") for m in mcids).strip()
        except Exception:
            return ""

    out: list[dict] = []
    fig_counter = [0]
    seen: set = set()

    def walk(node, depth=0):
        if depth > 30:
            return
        try:
            og = node.objgen
            if og != (0, 0):
                if og in seen:
                    return
                seen.add(og)
        except Exception:
            pass
        try:
            s = node.get("/S")
        except Exception:
            return
        if s is not None:
            tag = str(s).lstrip("/")
            entry = {"obj": node, "tag": tag, "text": mcid_text(node), "figure_no": 0}
            if tag == "Figure":
                fig_counter[0] += 1
                entry["figure_no"] = fig_counter[0]
                try:
                    entry["text"] = str(node.get("/Alt", "")) or entry["text"]
                except Exception:
                    pass
            out.append(entry)
        try:
            import pikepdf as _pk
            k = node.get("/K")
            kids = list(k) if isinstance(k, _pk.Array) else ([k] if k is not None else [])
            for kid in kids:
                if hasattr(kid, "get"):
                    walk(kid, depth + 1)
        except Exception:
            pass

    root = pdf.Root.get("/StructTreeRoot")
    if root is not None:
        walk(root)
    return out


# ---------------------------------------------------------------------------
# Fix application
# ---------------------------------------------------------------------------

def _apply_fixes(pdf_path: str, fixes: list[dict]) -> tuple[list[dict], list[dict]]:
    """Apply the dispatch-table fixes in place. Returns (applied, skipped)."""
    import pikepdf
    from pikepdf import Name, String

    applied: list[dict] = []
    skipped: list[dict] = []
    retagged = 0

    with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
        elements = _collect_elements(pdf)

        for fix in fixes:
            action = fix.get("action")
            try:
                if action == "retag":
                    new_tag = str(fix.get("new_tag", ""))
                    target = _norm(str(fix.get("target_text", "")))
                    if new_tag not in _ALLOWED_RETAGS or len(target) < 3:
                        skipped.append({**fix, "why": "invalid retag parameters"})
                        continue
                    matches = [e for e in elements if e["text"] and _norm(e["text"]) == target]
                    if not matches:  # fall back to prefix match (streams may truncate)
                        matches = [
                            e for e in elements
                            if e["text"] and (
                                _norm(e["text"]).startswith(target) or target.startswith(_norm(e["text"]))
                            ) and len(_norm(e["text"])) >= 3
                        ]
                    if len(matches) != 1:
                        skipped.append({**fix, "why": f"{len(matches)} elements matched — need exactly 1"})
                        continue
                    if matches[0]["tag"] == new_tag:
                        skipped.append({**fix, "why": "already tagged " + new_tag})
                        continue
                    matches[0]["obj"][Name("/S")] = Name("/" + new_tag)
                    matches[0]["tag"] = new_tag
                    retagged += 1
                    applied.append({
                        "action": "retag",
                        "text": str(fix.get("target_text", ""))[:80],
                        "to": new_tag,
                        "reason": fix.get("reason", ""),
                    })

                elif action == "set_alt":
                    n = int(fix.get("figure_number", 0) or 0)
                    new_alt = str(fix.get("new_alt", "")).strip()
                    figs = [e for e in elements if e["tag"] == "Figure"]
                    if n < 1 or n > len(figs) or not new_alt:
                        skipped.append({**fix, "why": "figure not found or empty alt"})
                        continue
                    figs[n - 1]["obj"][Name("/Alt")] = String(new_alt)
                    applied.append({
                        "action": "set_alt", "figure": n,
                        "alt": new_alt[:120], "reason": fix.get("reason", ""),
                    })

                elif action == "set_title":
                    title = str(fix.get("new_title", "")).strip()
                    if not title:
                        skipped.append({**fix, "why": "empty title"})
                        continue
                    pdf.docinfo[Name("/Title")] = String(title)
                    try:
                        with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                            meta["dc:title"] = title
                    except Exception:
                        pass
                    applied.append({"action": "set_title", "title": title[:120],
                                    "reason": fix.get("reason", "")})
                else:
                    skipped.append({**fix, "why": f"unknown action {action!r}"})
            except Exception as exc:
                log.warning("visual_fix apply %s: %s", action, exc)
                skipped.append({**fix, "why": f"error: {str(exc)[:80]}"})

        if applied:
            pdf.save()

    # Retagging can introduce heading-level skips (e.g. new H2s before an H3)
    # — run the existing heading repair pass to normalize.
    if retagged:
        try:
            from .patch_pdf import patch_heading_levels
            r = patch_heading_levels(pdf_path)
            if r.get("repairs_made"):
                applied.append({
                    "action": "heading_levels",
                    "count": r["repairs_made"],
                    "reason": "normalized heading hierarchy after retagging",
                })
        except Exception as exc:
            log.debug("visual_fix heading repair: %s", exc)

    return applied, skipped


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_visual_fix(pdf_path: str, max_pages: int = 6) -> dict:
    """Review the remediated PDF visually and auto-apply the safe fixes.

    Modifies pdf_path in place (with a validate-guard). Returns a report:
    {available, model, pagesReviewed, summary, applied[], skipped[], remaining[]}.
    """
    if os.environ.get("AI_VISUAL_FIX", "on").lower() in ("off", "0", "false"):
        return {"available": False, "reason": "disabled via AI_VISUAL_FIX env"}
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"available": False, "reason": "ANTHROPIC_API_KEY not configured on the engine."}
    try:
        import anthropic
    except ImportError:
        return {"available": False, "reason": "anthropic SDK not installed."}

    # Reuse the renderer from the review module; add per-element text so the
    # model can quote retag targets verbatim.
    from .ai_visual_check import _render_pages, _structure_digest

    try:
        pages = _render_pages(pdf_path, max_pages)
    except Exception as exc:
        return {"available": False, "reason": f"render failed: {exc}"}
    if not pages:
        return {"available": False, "reason": "No pages could be rendered."}

    digest = _structure_digest(pdf_path)
    try:
        import pikepdf
        with pikepdf.open(pdf_path) as pdf:
            digest["elements"] = [
                {"tag": e["tag"], "text": e["text"][:160]}
                for e in _collect_elements(pdf)
                if e["tag"] not in ("Document",)
            ][:200]
    except Exception as exc:
        log.debug("visual_fix element digest: %s", exc)

    import base64
    content: list[dict] = []
    for page_no, png in pages:
        content.append({"type": "text", "text": f"Page {page_no} render:"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png",
                       "data": base64.standard_b64encode(png).decode()},
        })
    content.append({
        "type": "text",
        "text": "Structure tree (tags in reading order, with each element's extracted text):\n"
                + json.dumps(digest, ensure_ascii=False),
    })

    client = anthropic.Anthropic(api_key=key)
    try:
        response = client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_INSTRUCTIONS,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:
        log.warning("visual_fix: API call failed: %s", exc)
        return {"available": False, "reason": f"AI review failed: {str(exc)[:200]}"}

    if response.stop_reason == "refusal":
        return {"available": False, "reason": "The model declined to review this document."}

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"available": False, "reason": "AI review returned unparseable output."}

    fixes = parsed.get("fixes", []) or []
    remaining = parsed.get("remaining", []) or []

    applied: list[dict] = []
    skipped: list[dict] = []
    if fixes:
        # Validate-guard: snapshot, apply, re-validate, revert if worse.
        from .validate import safe_validate_pdf
        before = safe_validate_pdf(pdf_path)
        fd, backup = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        shutil.copyfile(pdf_path, backup)
        try:
            applied, skipped = _apply_fixes(pdf_path, fixes)
            if applied:
                after = safe_validate_pdf(pdf_path)
                if (before.validation_error is None and after.validation_error is None
                        and after.failed_checks > before.failed_checks):
                    shutil.copyfile(backup, pdf_path)
                    skipped.extend({**a, "why": "reverted — fixes worsened veraPDF result"}
                                   for a in applied)
                    applied = []
        finally:
            if os.path.exists(backup):
                os.unlink(backup)

    return {
        "available": True,
        "model": _MODEL,
        "pagesReviewed": len(pages),
        "summary": parsed.get("summary", ""),
        "applied": applied,
        "skipped": [{k: v for k, v in s.items() if k in ("action", "target_text", "why")}
                    for s in skipped],
        "remaining": remaining,
        "disclaimer": (
            "AI visual review: confident visual mismatches were fixed automatically; "
            "items listed under 'remaining' still need a human decision."
        ),
    }
