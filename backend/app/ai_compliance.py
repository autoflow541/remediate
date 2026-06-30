"""ai_compliance.py — AI-driven loop to achieve PDF/UA-1 compliance.

Strategy
--------
1. Run veraPDF; collect failures.
2. Apply all known pikepdf patches for each failed clause.
3. If failures remain, use Claude to analyze them and recommend additional
   patches from a safe dispatch table (no code generation).
4. Re-validate.  Repeat up to `max_iterations` times.

Only terminates early if the PDF is already compliant or no progress was
made in a round (avoids infinite loops).

Returns (total_fixes_applied, notes_list).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from typing import Any

import pikepdf
from pikepdf import Dictionary, Name, String, Array

log = logging.getLogger(__name__)

# ── Anthropic client (optional — graceful fallback if key missing) ────────────
def _get_claude():
    try:
        import anthropic
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            return None
        return anthropic.Anthropic(api_key=key)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Low-level pikepdf patches — each returns True/int if something changed
# ═══════════════════════════════════════════════════════════════════════════════

def _set_mark_info(pdf: pikepdf.Pdf) -> bool:
    try:
        mi = pdf.Root.get("/MarkInfo")
        if mi is None or not bool(mi.get("/Marked", False)):
            pdf.Root.MarkInfo = Dictionary(Marked=True, Suspects=False)
            return True
    except Exception as e:
        log.debug("_set_mark_info: %s", e)
    return False


def _set_display_doc_title(pdf: pikepdf.Pdf) -> bool:
    try:
        vp = pdf.Root.get("/ViewerPreferences")
        if vp is None:
            pdf.Root.ViewerPreferences = Dictionary(DisplayDocTitle=True)
            return True
        if not bool(vp.get("/DisplayDocTitle", False)):
            vp[Name("/DisplayDocTitle")] = True
            pdf.Root.ViewerPreferences = vp
            return True
    except Exception as e:
        log.debug("_set_display_doc_title: %s", e)
    return False


def _set_catalog_lang(pdf: pikepdf.Pdf, lang: str = "en") -> bool:
    try:
        existing = pdf.Root.get("/Lang")
        if not existing or str(existing).strip() == "":
            pdf.Root.Lang = String(lang)
            return True
    except Exception as e:
        log.debug("_set_catalog_lang: %s", e)
    return False


def _set_title(pdf: pikepdf.Pdf, title: str) -> bool:
    """Set /Title in both docinfo and XMP metadata."""
    if not title:
        return False
    try:
        changed = False
        with pdf.open_metadata() as meta:
            if not meta.get("dc:title"):
                meta["dc:title"] = title
                changed = True
        info = pdf.docinfo
        if not str(info.get("/Title", "")).strip():
            info[Name("/Title")] = String(title)
            changed = True
        return changed
    except Exception as e:
        log.debug("_set_title: %s", e)
    return False


def _set_tabs_s(pdf: pikepdf.Pdf) -> int:
    """Add /Tabs /S to annotated pages missing tab order."""
    fixed = 0
    try:
        for page in pdf.pages:
            if page.get("/Annots") and not page.get("/Tabs"):
                page[Name("/Tabs")] = Name("/S")
                fixed += 1
    except Exception as e:
        log.debug("_set_tabs_s: %s", e)
    return fixed


def _fix_th_scope(pdf: pikepdf.Pdf) -> int:
    """Walk struct tree and assign /Scope to TH elements missing it."""
    count = 0
    try:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if not struct_root:
            return 0

        def _walk(elem, row_idx=0, col_idx=0, in_table=False):
            nonlocal count
            try:
                if not hasattr(elem, "get"):
                    return
                tag = str(elem.get("/S", ""))

                if tag == "/Table":
                    # Walk table rows
                    kids = _kids(elem)
                    r = 0
                    for kid in kids:
                        t = str(kid.get("/S", "")) if hasattr(kid, "get") else ""
                        if t in ("/THead", "/TBody", "/TFoot"):
                            for row in _kids(kid):
                                if hasattr(row, "get") and str(row.get("/S", "")) == "/TR":
                                    _walk_row(row, r)
                                    r += 1
                        elif t == "/TR":
                            _walk_row(kid, r)
                            r += 1
                    return

                for kid in _kids(elem):
                    _walk(kid)
            except Exception:
                pass

        def _walk_row(row_elem, row_idx: int):
            nonlocal count
            col = 0
            for cell in _kids(row_elem):
                try:
                    if not hasattr(cell, "get"):
                        col += 1
                        continue
                    tag = str(cell.get("/S", ""))
                    if tag == "/TH":
                        scope = _get_scope(cell)
                        if scope is None:
                            if row_idx == 0 and col == 0:
                                new_scope = "Both"
                            elif row_idx == 0:
                                new_scope = "Column"
                            else:
                                new_scope = "Row"
                            _set_scope(cell, new_scope)
                            count += 1
                except Exception:
                    pass
                col += 1

        def _kids(obj) -> list:
            try:
                k = obj.get("/K")
                if k is None:
                    return []
                if isinstance(k, Array):
                    out = []
                    for item in k:
                        try:
                            out.append(item.get_object() if hasattr(item, "get_object") else item)
                        except Exception:
                            pass
                    return out
                if isinstance(k, pikepdf.Dictionary):
                    return [k]
                if hasattr(k, "get_object"):
                    return [k.get_object()]
                return []
            except Exception:
                return []

        def _get_scope(th) -> str | None:
            try:
                a = th.get("/A")
                if a is None:
                    return None
                candidates = list(a) if isinstance(a, Array) else [a]
                for attr in candidates:
                    try:
                        resolved = attr.get_object() if hasattr(attr, "get_object") else attr
                        if hasattr(resolved, "get"):
                            s = resolved.get("/Scope")
                            if s is not None:
                                return str(s)
                    except Exception:
                        pass
            except Exception:
                pass
            return None

        def _set_scope(th, scope: str):
            try:
                attr_dict = Dictionary(
                    O=Name("/Table"),
                    Scope=Name(f"/{scope}"),
                )
                existing = th.get("/A")
                if existing is None:
                    th[Name("/A")] = attr_dict
                elif isinstance(existing, Array):
                    existing.append(attr_dict)
                    th[Name("/A")] = existing
                else:
                    th[Name("/A")] = Array([existing, attr_dict])
            except Exception as e:
                log.debug("_set_scope: %s", e)

        _walk(struct_root)
    except Exception as e:
        log.debug("_fix_th_scope: %s", e)
    return count


def _fix_annot_contents(pdf: pikepdf.Pdf) -> int:
    """Add /Contents to annotations missing it."""
    fixed = 0
    try:
        for page in pdf.pages:
            annots = page.get("/Annots")
            if not annots:
                continue
            for ref in annots:
                try:
                    annot = ref.get_object() if hasattr(ref, "get_object") else ref
                    if not hasattr(annot, "get"):
                        continue
                    if annot.get("/Contents") is not None:
                        continue
                    subtype = str(annot.get("/Subtype", ""))
                    # Set a sensible default
                    if subtype == "/Link":
                        # Use URI if available
                        action = annot.get("/A")
                        uri = ""
                        if action and hasattr(action, "get"):
                            uri = str(action.get("/URI", ""))
                        annot[Name("/Contents")] = String(uri or "Link")
                    elif subtype == "/Widget":
                        t = ""
                        try:
                            t = str(annot.get("/T", ""))
                        except Exception:
                            pass
                        annot[Name("/Contents")] = String(t or "Form field")
                    else:
                        annot[Name("/Contents")] = String("")
                    fixed += 1
                except Exception:
                    pass
    except Exception as e:
        log.debug("_fix_annot_contents: %s", e)
    return fixed


def _fix_figure_alt(pdf: pikepdf.Pdf) -> int:
    """Replace empty/missing /Alt on Figure struct elements."""
    count = 0
    try:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if not struct_root:
            return 0

        def _walk(elem):
            nonlocal count
            try:
                if not hasattr(elem, "get"):
                    return
                if str(elem.get("/S", "")) in ("/Figure", "/Formula"):
                    alt = elem.get("/Alt")
                    if alt is None or str(alt).strip() == "":
                        elem[Name("/Alt")] = String("Image")
                        count += 1
                k = elem.get("/K")
                if k is None:
                    return
                if isinstance(k, Array):
                    for item in k:
                        try:
                            _walk(item.get_object() if hasattr(item, "get_object") else item)
                        except Exception:
                            pass
                elif isinstance(k, pikepdf.Dictionary):
                    _walk(k)
                elif hasattr(k, "get_object"):
                    _walk(k.get_object())
            except Exception:
                pass

        _walk(struct_root)
    except Exception as e:
        log.debug("_fix_figure_alt: %s", e)
    return count


def _fix_struct_elem_id(pdf: pikepdf.Pdf) -> int:
    """Remove duplicate /ID values from struct elements (veraPDF 7.9)."""
    seen_ids: set[str] = set()
    count = 0
    try:
        struct_root = pdf.Root.get("/StructTreeRoot")
        if not struct_root:
            return 0

        def _walk(elem):
            nonlocal count
            try:
                if not hasattr(elem, "get"):
                    return
                eid = elem.get("/ID")
                if eid is not None:
                    eid_str = str(eid)
                    if eid_str in seen_ids:
                        del elem[Name("/ID")]
                        count += 1
                    else:
                        seen_ids.add(eid_str)
                k = elem.get("/K")
                if k is None:
                    return
                items = list(k) if isinstance(k, Array) else ([k] if isinstance(k, pikepdf.Dictionary) else [])
                for item in items:
                    try:
                        _walk(item.get_object() if hasattr(item, "get_object") else item)
                    except Exception:
                        pass
            except Exception:
                pass

        _walk(struct_root)
    except Exception as e:
        log.debug("_fix_struct_elem_id: %s", e)
    return count


def _add_output_intent(pdf: pikepdf.Pdf) -> bool:
    """Add a minimal sRGB output intent if none exists."""
    try:
        if pdf.Root.get("/OutputIntents"):
            return False
        srgb_intent = Dictionary(
            Type=Name("/OutputIntent"),
            S=Name("/GTS_PDFA1"),
            OutputConditionIdentifier=String("sRGB"),
            RegistryName=String("http://www.color.org"),
            Info=String("sRGB IEC61966-2.1"),
        )
        pdf.Root.OutputIntents = Array([srgb_intent])
        return True
    except Exception as e:
        log.debug("_add_output_intent: %s", e)
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# Known clause → patch dispatch
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_known_patches(pdf: pikepdf.Pdf, failed_clauses: set[str], lang: str = "en") -> tuple[int, list[str]]:
    """Apply all patches we know how to do for given failed clauses.
    Returns (count, notes)."""
    count = 0
    notes: list[str] = []

    def _check(fn, label: str, *args) -> bool:
        try:
            result = fn(pdf, *args) if args else fn(pdf)
            if result:
                notes.append(label)
                return True
        except Exception as e:
            log.debug("patch %s: %s", label, e)
        return False

    # Always-safe fixes (apply regardless of clause)
    n_tabs = _set_tabs_s(pdf)
    if n_tabs:
        count += n_tabs
        notes.append(f"/Tabs /S added to {n_tabs} page(s)")

    # Clause-targeted fixes
    if any(c.startswith("7.1") for c in failed_clauses):
        if _check(_set_mark_info, "MarkInfo /Marked=true"):
            count += 1
        if _check(_set_display_doc_title, "ViewerPreferences /DisplayDocTitle=true"):
            count += 1

    if any(c.startswith("7.2") for c in failed_clauses) or "7.1-2" in failed_clauses:
        if _check(lambda p: _set_catalog_lang(p, lang), f"Catalog /Lang set to {lang!r}"):
            count += 1

    if any(c.startswith("7.3") for c in failed_clauses):
        n_alt = _fix_figure_alt(pdf)
        if n_alt:
            count += n_alt
            notes.append(f"/Alt set on {n_alt} Figure element(s)")

    if any(c.startswith("7.5") for c in failed_clauses) or any(c.startswith("7.4") for c in failed_clauses):
        n_th = _fix_th_scope(pdf)
        if n_th:
            count += n_th
            notes.append(f"/Scope assigned to {n_th} TH element(s)")

    if any(c.startswith("7.18") for c in failed_clauses):
        n_annot = _fix_annot_contents(pdf)
        if n_annot:
            count += n_annot
            notes.append(f"/Contents added to {n_annot} annotation(s)")

    # Structural duplicates
    n_id = _fix_struct_elem_id(pdf)
    if n_id:
        count += n_id
        notes.append(f"Removed {n_id} duplicate struct /ID(s)")

    return count, notes


# ═══════════════════════════════════════════════════════════════════════════════
# Claude analysis pass
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_pdf_context(pdf_path: str) -> dict:
    """Extract metadata and text snippet for Claude's context."""
    ctx: dict[str, Any] = {"lang": "", "title": "", "text_sample": "", "has_tables": False, "has_figures": False}
    try:
        import fitz
        doc = fitz.open(pdf_path)
        ctx["text_sample"] = doc[0].get_text()[:500] if doc.page_count else ""
        doc.close()
    except Exception:
        pass
    try:
        pdf = pikepdf.open(pdf_path)
        ctx["lang"] = str(pdf.Root.get("/Lang", "")).strip()
        ctx["title"] = str(pdf.docinfo.get("/Title", "")).strip()

        def _walk_struct(elem, depth=0):
            if depth > 5 or not hasattr(elem, "get"):
                return
            tag = str(elem.get("/S", ""))
            if tag in ("/Table", "/TR", "/TH", "/TD"):
                ctx["has_tables"] = True
            if tag in ("/Figure",):
                ctx["has_figures"] = True
            k = elem.get("/K")
            if k and isinstance(k, Array):
                for item in k:
                    try:
                        _walk_struct(item.get_object() if hasattr(item, "get_object") else item, depth + 1)
                    except Exception:
                        pass

        sr = pdf.Root.get("/StructTreeRoot")
        if sr:
            _walk_struct(sr)
        pdf.close()
    except Exception:
        pass
    return ctx


def _claude_compliance_pass(pdf_path: str, failures: list[dict]) -> tuple[int, list[str]]:
    """Ask Claude to recommend patches for remaining veraPDF failures.
    Returns (fixes_applied, notes)."""
    client = _get_claude()
    if not client:
        return 0, []

    ctx = _extract_pdf_context(pdf_path)

    # Build compact failure summary
    failure_lines = []
    for f in failures:
        clause = f.get("clause", "?")
        test_n = f.get("test_number", "?")
        desc = f.get("description", "") or f.get("message", "")
        failure_lines.append(f"  clause {clause}-{test_n}: {desc}")
    failure_text = "\n".join(failure_lines) or "  (no details)"

    prompt = f"""You are a PDF accessibility expert helping achieve PDF/UA-1 compliance.

PDF context:
- Catalog /Lang: {ctx['lang'] or '(missing)'}
- Document title: {ctx['title'] or '(missing)'}
- Has tables: {ctx['has_tables']}
- Has figures: {ctx['has_figures']}
- Text sample: {ctx['text_sample'][:300]}

Remaining veraPDF failures after standard repairs:
{failure_text}

Respond with a JSON object containing:
{{
  "lang": "<BCP-47 language code to set, or null if already set>",
  "title": "<title string to set, or null if already set or undetectable>",
  "fix_th_scope": <true if TH /Scope should be set>,
  "fix_annot_contents": <true if annotation /Contents should be patched>,
  "fix_figure_alt": <true if Figure /Alt should be patched>,
  "fix_mark_info": <true if MarkInfo should be set>,
  "fix_display_doc_title": <true if ViewerPreferences /DisplayDocTitle should be set>,
  "notes": ["<brief explanation of each decision>"]
}}

Only include keys that need action. Detect language from the text sample if /Lang is missing."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Extract JSON
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return 0, []
        rec = json.loads(m.group())
    except Exception as e:
        log.warning("Claude compliance analysis failed: %s", e)
        return 0, []

    # Apply Claude's recommendations
    count = 0
    notes: list[str] = rec.get("notes", [])

    try:
        pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)

        if rec.get("fix_mark_info"):
            if _set_mark_info(pdf):
                count += 1

        if rec.get("fix_display_doc_title"):
            if _set_display_doc_title(pdf):
                count += 1

        lang = rec.get("lang")
        if lang:
            if _set_catalog_lang(pdf, lang):
                count += 1
                notes.append(f"Claude set /Lang to {lang!r}")

        title = rec.get("title")
        if title:
            if _set_title(pdf, title):
                count += 1
                notes.append(f"Claude set title to {title!r}")

        if rec.get("fix_th_scope"):
            n = _fix_th_scope(pdf)
            count += n
            if n:
                notes.append(f"Claude: /Scope set on {n} TH element(s)")

        if rec.get("fix_annot_contents"):
            n = _fix_annot_contents(pdf)
            count += n
            if n:
                notes.append(f"Claude: /Contents added to {n} annotation(s)")

        if rec.get("fix_figure_alt"):
            n = _fix_figure_alt(pdf)
            count += n
            if n:
                notes.append(f"Claude: /Alt patched on {n} Figure(s)")

        if count > 0:
            pdf.save(pdf_path)
        pdf.close()
    except Exception as e:
        log.warning("Claude compliance apply failed: %s", e)

    return count, notes


# ═══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════════

def fix_compliance_issues(pdf_path: str, max_iterations: int = 3) -> tuple[int, list[str]]:
    """Loop: validate → patch known issues → Claude analysis → re-validate.

    Returns (total_fixes, all_notes).
    """
    total_fixes = 0
    all_notes: list[str] = []

    # Detect document language once (used for /Lang default)
    auto_lang = "en"
    try:
        import fitz
        doc = fitz.open(pdf_path)
        sample = " ".join(p.get_text() for p in doc)[:2000]
        doc.close()
        from .language_fix import _detect_lang
        detected = _detect_lang(sample)
        if detected:
            auto_lang = detected
    except Exception:
        pass

    for iteration in range(1, max_iterations + 1):
        # ── Validate ──────────────────────────────────────────────────────────
        try:
            from .validate import validate_pdf
            result = validate_pdf(pdf_path, flavour="ua1")
            if result.compliant:
                all_notes.append(f"PDF/UA-1 compliant after iteration {iteration - 1} ✓")
                break
            failures_dicts = [
                {
                    "clause": r.clause,
                    "test_number": r.test_number,
                    "description": r.description,
                    "failed_checks": r.failed_checks,
                }
                for r in result.failures
            ]
            failed_clauses = {r.clause for r in result.failures}
        except Exception as e:
            log.warning("compliance_loop[%d]: validate failed: %s", iteration, e)
            break

        log.info("compliance_loop[%d]: %d failures — clauses %s",
                 iteration, len(failures_dicts), sorted(failed_clauses))

        # ── Known patches ─────────────────────────────────────────────────────
        round_fixes = 0
        try:
            pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)
            n, notes = _apply_known_patches(pdf, failed_clauses, lang=auto_lang)
            if n > 0:
                pdf.save(pdf_path)
            pdf.close()
            round_fixes += n
            all_notes.extend(notes)
        except Exception as e:
            log.warning("compliance_loop[%d]: known patches failed: %s", iteration, e)

        # ── Claude analysis pass ──────────────────────────────────────────────
        try:
            n, notes = _claude_compliance_pass(pdf_path, failures_dicts)
            round_fixes += n
            all_notes.extend(notes)
        except Exception as e:
            log.warning("compliance_loop[%d]: Claude pass failed: %s", iteration, e)

        total_fixes += round_fixes

        if round_fixes == 0:
            all_notes.append(f"No further fixes possible after {iteration} iteration(s)")
            break

    return total_fixes, all_notes
