"""quickfix.py — apply all AI-driven auto-fixes to an already-remediated PDF.

Runs in a safe, ordered sequence.  Each fixer is wrapped in try/except so a
single failure does not abort the rest.  The PDF is modified in-place at
pdf_path.

Fix sequence
------------
1.  Background fill whitening   — strips colored page fills that hurt contrast
2.  Text contrast repair        — darken text that fails WCAG 1.4.3 (4.5:1 / 3:1)
3.  Non-text contrast repair    — widget borders / graphic strokes (WCAG 1.4.11)
4.  Heading level repair        — close skipped H levels (H1→H3 → H1→H2→H3)
5.  Table header scope          — assign /Scope Column/Row/Both to TH cells
6.  Metadata auto-fill          — set /Lang and /Title if absent
7.  Font embedding              — embed / subset unembedded fonts
8.  Language tag injection      — add /Lang to foreign-language struct elements
9.  Figure alt text generation  — Vision AI fills empty Figure /Alt tags
10. veraPDF auto-repair         — patch known veraPDF rule failures

Returns a summary dict consumed by the /quickfix endpoint.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)


def _try(label: str, fn, *args, **kwargs):
    """Call fn(*args, **kwargs), return (result, elapsed_ms).  Never raises."""
    t0 = time.perf_counter()
    try:
        result = fn(*args, **kwargs)
        return result, int((time.perf_counter() - t0) * 1000)
    except Exception as exc:
        log.warning("quickfix[%s]: %s", label, exc)
        return None, int((time.perf_counter() - t0) * 1000)


def run_quickfix(pdf_path: str) -> dict:
    """Apply all auto-fixes to pdf_path (in-place).  Return summary dict."""

    summary: dict = {
        "ok": True,
        "fixes": {},
        "notes": [],
        "errors": [],
    }

    def _record(key: str, result, notes_list=None):
        summary["fixes"][key] = result
        if notes_list:
            summary["notes"].extend(notes_list)

    # ── 1. Background fill whitening ─────────────────────────────────────────
    try:
        from .clean_background import clean_background_fills
        import tempfile, os
        fd, tmp = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        count = clean_background_fills(pdf_path, tmp)
        if count > 0:
            os.replace(tmp, pdf_path)
            _record("backgroundFillsWhitened", count,
                    [f"Whitened {count} colored background fill(s) — contrast improved"])
        else:
            os.unlink(tmp)
            _record("backgroundFillsWhitened", 0)
    except Exception as exc:
        summary["errors"].append(f"background_fills: {exc}")
        _record("backgroundFillsWhitened", 0)

    # ── 2. Text contrast repair ───────────────────────────────────────────────
    try:
        from .contrast import check_contrast
        from .contrast_fix import fix_contrast
        failures = check_contrast(pdf_path)
        if failures:
            n, notes = fix_contrast(pdf_path, failures)
            _record("contrastTextFixed", n, notes)
        else:
            _record("contrastTextFixed", 0)
    except Exception as exc:
        summary["errors"].append(f"contrast_text: {exc}")
        _record("contrastTextFixed", 0)

    # ── 3. Non-text contrast repair ───────────────────────────────────────────
    try:
        from .nontext_contrast import check_nontext_contrast
        from .contrast_fix import fix_nontext_contrast  # noqa: F401 — exists in contrast_fix.py
        nt_issues = check_nontext_contrast(pdf_path)
        if nt_issues:
            n, notes = fix_nontext_contrast(pdf_path, nt_issues)
            _record("contrastNontextFixed", n, notes)
        else:
            _record("contrastNontextFixed", 0)
    except Exception as exc:
        summary["errors"].append(f"contrast_nontext: {exc}")
        _record("contrastNontextFixed", 0)

    # ── 4. Heading level repair ───────────────────────────────────────────────
    try:
        from .patch_pdf import patch_heading_levels
        r = patch_heading_levels(pdf_path)
        repaired = r.get("repairs_made", 0)
        _record("headingsRepaired", repaired,
                [f"Heading hierarchy repaired: {repaired} level(s) fixed"] if repaired else [])
    except Exception as exc:
        summary["errors"].append(f"headings: {exc}")
        _record("headingsRepaired", 0)

    # ── 5. Table header scope ─────────────────────────────────────────────────
    try:
        from .patch_pdf import patch_table_headers
        r = patch_table_headers(pdf_path)
        tagged = r.get("cells_tagged", 0)
        _record("tableHeadersScoped", tagged,
                [f"Table header scope: {tagged} TH cell(s) assigned /Scope"] if tagged else [])
    except Exception as exc:
        summary["errors"].append(f"table_scope: {exc}")
        _record("tableHeadersScoped", 0)

    # ── 6. Metadata auto-fill ─────────────────────────────────────────────────
    try:
        import pikepdf
        pdf_tmp = pikepdf.open(pdf_path)
        needs_lang  = not str(pdf_tmp.Root.get("/Lang", "")).strip()
        needs_title = not str(pdf_tmp.docinfo.get("/Title", "")).strip()
        pdf_tmp.close()

        meta_fixed = 0
        if needs_lang or needs_title:
            from .patch_pdf import patch_metadata
            # Auto-detect language from body text
            auto_lang = "en"
            try:
                import fitz
                d = fitz.open(pdf_path)
                sample = " ".join(p.get_text() for p in d)[:2000]
                d.close()
                from .language_fix import _detect_lang
                detected = _detect_lang(sample)
                if detected:
                    auto_lang = detected
            except Exception:
                pass
            # Auto-detect title from first H1 in struct tree
            auto_title = ""
            try:
                import fitz
                d = fitz.open(pdf_path)
                for page in d:
                    blocks = page.get_text("dict", flags=0).get("blocks", [])
                    for b in blocks:
                        for ln in b.get("lines", []):
                            for sp in ln.get("spans", []):
                                if sp.get("size", 0) >= 16 and sp.get("text", "").strip():
                                    auto_title = sp["text"].strip()
                                    break
                            if auto_title:
                                break
                        if auto_title:
                            break
                    if auto_title:
                        break
                d.close()
            except Exception:
                pass

            r = patch_metadata(
                pdf_path,
                title=auto_title if needs_title else "",
                lang=auto_lang if needs_lang else "",
            )
            if r.get("ok"):
                meta_fixed = len(r.get("patched_fields", []))
                if meta_fixed:
                    _record("metadataFixed", meta_fixed,
                            [f"Metadata: set {r['patched_fields']} (auto-detected)"])
        if not (needs_lang or needs_title):
            _record("metadataFixed", 0)
    except Exception as exc:
        summary["errors"].append(f"metadata: {exc}")
        _record("metadataFixed", 0)

    # ── 7. Font embedding ─────────────────────────────────────────────────────
    try:
        from .font_check import check_fonts
        from .font_embed import embed_fonts
        font_issues = check_fonts(pdf_path)
        # Always call embed_fonts — if font_issues is empty, it scans & embeds all unembedded fonts
        n, notes = embed_fonts(pdf_path, font_issues)
        _record("fontsEmbedded", n, notes)
    except Exception as exc:
        summary["errors"].append(f"fonts: {exc}")
        _record("fontsEmbedded", 0)

    # ── 7a. Structure-tree content repairs (PDF/UA 7.1) ──────────────────────
    # Order matters: untangle artifact/tagged interleavings first (7.1-1/2),
    # then reconnect orphaned islands and unreferenced MCIDs (7.1-3), and only
    # then artifact-wrap whatever is still genuinely unmarked (7.1-3).
    try:
        from .interleave_fix import fix_interleaved_marked_content
        n, notes = fix_interleaved_marked_content(pdf_path)
        _record("interleavingsFixed", n, notes)
    except Exception as exc:
        summary["errors"].append(f"interleave: {exc}")
        _record("interleavingsFixed", 0)

    try:
        from .orphan_mcid_fix import adopt_orphaned_mcids
        n, notes = adopt_orphaned_mcids(pdf_path)
        _record("orphanedMcidsAdopted", n, notes)
    except Exception as exc:
        summary["errors"].append(f"orphan_mcid: {exc}")
        _record("orphanedMcidsAdopted", 0)

    try:
        from .artifact_wrap import wrap_unmarked_content
        n, notes = wrap_unmarked_content(pdf_path)
        _record("unmarkedContentWrapped", n, notes)
    except Exception as exc:
        summary["errors"].append(f"artifact_wrap: {exc}")
        _record("unmarkedContentWrapped", 0)

    # ── 7b. Annotation alternate descriptions (PDF/UA 7.18.1 / 7.18.5) ───────
    try:
        from .annot_alt_fix import fix_annotation_descriptions
        n, notes = fix_annotation_descriptions(pdf_path)
        _record("annotDescriptionsAdded", n, notes)
    except Exception as exc:
        summary["errors"].append(f"annot_descriptions: {exc}")
        _record("annotDescriptionsAdded", 0)

    # ── 7c. Incomplete /CIDSet removal (PDF/UA 7.21.4.2) ─────────────────────
    try:
        from .cidset_fix import remove_incomplete_cidsets
        n, notes = remove_incomplete_cidsets(pdf_path)
        _record("cidsetsRemoved", n, notes)
    except Exception as exc:
        summary["errors"].append(f"cidset: {exc}")
        _record("cidsetsRemoved", 0)

    # ── 8. Language tag injection ─────────────────────────────────────────────
    try:
        from .language_fix import fix_language_tags
        n, notes = fix_language_tags(pdf_path)
        _record("langTagsAdded", n, notes)
    except Exception as exc:
        summary["errors"].append(f"language_tags: {exc}")
        _record("langTagsAdded", 0)

    # ── 9. Figure alt text generation ────────────────────────────────────────
    try:
        from .alt_fix import fix_alt_text
        n, notes = fix_alt_text(pdf_path)
        _record("altTextGenerated", n, notes)
    except Exception as exc:
        summary["errors"].append(f"alt_text: {exc}")
        _record("altTextGenerated", 0)

    # ── 10. veraPDF auto-repair ───────────────────────────────────────────────
    try:
        from .validate import validate_pdf
        from .verapdf_auto_repair import auto_repair
        result = validate_pdf(pdf_path)
        if not result.compliant and result.failures:
            failures_raw = [f.__dict__ if hasattr(f, "__dict__") else f
                            for f in result.failures]
            n, notes = auto_repair(pdf_path, failures_raw)
            _record("verapdfRepairs", n, notes)
        else:
            _record("verapdfRepairs", 0)
    except Exception as exc:
        summary["errors"].append(f"verapdf_repair: {exc}")
        _record("verapdfRepairs", 0)

    # ── 11. AI compliance loop ────────────────────────────────────────────────
    try:
        from .ai_compliance import fix_compliance_issues
        n, notes = fix_compliance_issues(pdf_path, max_iterations=3)
        _record("aiComplianceFixes", n, notes)
    except Exception as exc:
        summary["errors"].append(f"ai_compliance: {exc}")
        _record("aiComplianceFixes", 0)

    # ── Tally total fixes ─────────────────────────────────────────────────────
    summary["totalFixes"] = sum(
        v for v in summary["fixes"].values() if isinstance(v, int)
    )
    log.info(
        "quickfix: %d total fixes applied  errors=%d",
        summary["totalFixes"], len(summary["errors"]),
    )
    return summary
