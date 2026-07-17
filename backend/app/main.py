"""FastAPI app for the PDF accessibility remediation engine."""
from __future__ import annotations
import logging, os, re, tempfile, time, json
from .log_config import configure as _configure_logging
_configure_logging()
log = logging.getLogger(__name__)
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from .autotag import AutotagError, autotag_pdf
from .manifest import count_nodes
from .validate import DEFAULT_FLAVOUR, KNOWN_FLAVOURS, VeraPDFError, get_verapdf_version, validate_pdf, safe_validate_pdf
from .writeback import WritebackError, remediate_pdf

_API_KEY = os.environ.get("API_KEY", "")  # empty string = open access (no auth required)

_TAGS = [
    {"name": "core",    "description": "Submit PDFs, get accessibility manifests and remediated output."},
    {"name": "patch",   "description": "One-click post-remediation fixes (metadata, headings, tables)."},
    {"name": "reorder", "description": "Reading order extraction and rewrite."},
    {"name": "batch",   "description": "Multi-file batch remediation."},
    {"name": "status",  "description": "Health check and version info."},
]

app = FastAPI(
    title="PDF Accessibility Remediation API",
    version="0.2.0",
    description=(
        "Automate PDF/UA-1 and WCAG 2.2 AA remediation.\n\n"
        "**Authentication:** if the server is configured with an `API_KEY` environment variable, "
        "all requests (except `/health` and the docs) must include an `X-API-Key` header.\n\n"
        "**File size limit:** 100 MB per upload (configurable via `MAX_UPLOAD_BYTES`)."
    ),
    openapi_tags=_TAGS,
)
_origins = os.environ.get("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins if o.strip()],
    allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
    expose_headers=[
        "X-Conformance","X-VeraPDF-Compliant","X-VeraPDF-Failed-Rules",
        "X-Remediation-Elements","X-Remediation-MCIDs",
        "X-Patch-Result","X-Reorder-Result","X-QuickFix-Result","Content-Disposition",
    ],
)

_OPEN_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}

@app.middleware("http")
async def _auth(request: Request, call_next):
    """Enforce API key when API_KEY env var is set."""
    if _API_KEY and request.url.path not in _OPEN_PATHS:
        provided = request.headers.get("X-API-Key", "")
        if provided != _API_KEY:
            return JSONResponse({"detail": "Invalid or missing API key. Pass X-API-Key header."}, status_code=401)
    return await call_next(request)

@app.middleware("http")
async def _log_requests(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000
    log.info("%s %s -> %d  (%.0f ms)", request.method, request.url.path, response.status_code, ms)
    return response

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
MAX_BATCH_FILES  = int(os.environ.get("MAX_BATCH_FILES",  "20"))

def _safe_filename(name: str) -> str:
    name = re.sub(r'[\r\n"\\]', '_', name)
    return name[:200]

def _save_upload(upload: UploadFile) -> str:
    fd, path = tempfile.mkstemp(suffix=".pdf")
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk: break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail=f"File exceeds {MAX_UPLOAD_BYTES} byte limit.")
                out.write(chunk)
    except Exception:
        if os.path.exists(path): os.unlink(path)
        raise
    return path

@app.get("/health", tags=["status"], summary="Health check — returns veraPDF version")
def health() -> dict:
    version = get_verapdf_version()
    return {"status": "ok", "verapdf": version, "verapdf_available": version is not None}

@app.post("/validate", tags=["core"], summary="Validate a PDF against PDF/UA-1 or WCAG using veraPDF")
def validate(file: UploadFile = File(...), flavour: str = Form(DEFAULT_FLAVOUR)) -> JSONResponse:
    if flavour not in KNOWN_FLAVOURS:
        raise HTTPException(status_code=400, detail=f"Unknown flavour {flavour!r}.")
    path = _save_upload(file)
    try:
        # safe_validate_pdf never raises for a veraPDF internal crash — it
        # returns an "unavailable" result so /validate answers 200 with
        # validation_error instead of a 500.
        result = safe_validate_pdf(path, flavour=flavour)
    finally:
        if os.path.exists(path): os.unlink(path)
    return JSONResponse(result.to_dict())

@app.post("/autotag", tags=["core"], summary="Analyse a PDF and return a structured accessibility manifest (JSON)")
def autotag(file: UploadFile = File(...), detect_headers: bool = Form(True)) -> JSONResponse:
    path = _save_upload(file)
    try:
        manifest = autotag_pdf(path, detect_headers=detect_headers)
    except AutotagError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if os.path.exists(path): os.unlink(path)
    if file.filename: manifest["source"]["filename"] = file.filename
    manifest["source"]["nodeCount"] = count_nodes(manifest["nodes"])
    return JSONResponse(manifest)

@app.post("/remediate", tags=["core"], summary="Remediate a PDF using a manifest — returns a tagged, accessible PDF")
def remediate(file: UploadFile = File(...), manifest: UploadFile = File(...), flavour: str = Form(DEFAULT_FLAVOUR)) -> Response:
    try:
        manifest_obj = json.loads(manifest.file.read())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid manifest JSON: {exc}")
    if not isinstance(manifest_obj, dict):
        raise HTTPException(status_code=400, detail="Manifest must be a JSON object.")
    in_path = _save_upload(file)
    fd, out_path = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
    fd2, fixed_path = tempfile.mkstemp(suffix=".pdf"); os.close(fd2)
    contrast_fixes = 0
    t0 = time.monotonic()
    log.info("REMEDIATE start  file=%r", file.filename)
    baseline_result = None
    regression_guard = {"triggered": False}
    try:
        # Baseline conformance of the INPUT — feeds the regression guard so we
        # never hand back a file worse than we received.
        try:
            baseline_result = validate_pdf(in_path, flavour=flavour)
        except Exception:
            baseline_result = None
        # Already-tagged PDFs are REPAIRED (structure tree preserved), not
        # rebuilt from scratch — rebuilding a good tree regresses the document.
        from .tagged_check import assess_tagging, repair_tagged
        tag_info = assess_tagging(in_path)
        if tag_info.get("tagged"):
            log.info("REMEDIATE repair-mode: %s", tag_info.get("reason"))
            report = repair_tagged(in_path, out_path, manifest_obj)
        else:
            report = remediate_pdf(in_path, manifest_obj, out_path)
        report["taggingAssessment"] = tag_info
        try:
            from .clean_background import clean_background_fills
            fd3, bg_path = tempfile.mkstemp(suffix=".pdf"); os.close(fd3)
            bg_fixes = clean_background_fills(out_path, bg_path)
            if bg_fixes > 0: os.replace(bg_path, out_path); report["backgroundFillsWhitened"] = bg_fixes
            else: os.unlink(bg_path); report["backgroundFillsWhitened"] = 0
        except Exception as _bg_exc:
            log.warning("Background cleanup skipped: %s", _bg_exc); report["backgroundFillsWhitened"] = 0
        try:
            from .fix_contrast import fix_contrast_colors
            contrast_fixes = fix_contrast_colors(out_path, fixed_path)
            if contrast_fixes > 0: os.replace(fixed_path, out_path); fixed_path = None
            else: os.unlink(fixed_path); fixed_path = None
        except Exception: pass
        form_total = 0; form_fixed = 0; form_fields: list = []
        try:
            from .form_fields import remediate_form_fields
            form_total, form_fixed, form_fields = remediate_form_fields(out_path)
        except Exception: pass
        result = safe_validate_pdf(out_path, flavour=flavour)
        contrast_failures: list = []
        try:
            from .contrast import check_contrast
            contrast_failures = check_contrast(out_path)
        except Exception: pass
        link_quality_issues: list = []; link_quality_auto_fixed: list = []
        try:
            from .link_quality import check_link_quality
            raw_link_issues = check_link_quality(out_path)
            from .fix_link_text import generate_link_description
            for issue in raw_link_issues:
                url = issue.get("url", "")
                if url: link_quality_auto_fixed.append({**issue, "autoFixedAlt": generate_link_description(url, issue.get("text",""))})
                else: link_quality_issues.append(issue)
        except Exception: pass
        alt_issues: list = []
        try:
            from .alt_quality import check_alt_quality; alt_issues = check_alt_quality(out_path)
        except Exception: pass
        color_only_warnings: list = []
        try:
            from .color_only import detect_color_only; color_only_warnings = detect_color_only(out_path)
        except Exception: pass
        heading_issues: list = []
        try:
            from .heading_check import check_headings; heading_issues = check_headings(out_path)
        except Exception: pass
        sensory_issues: list = []
        try:
            from .sensory_check import check_sensory; sensory_issues = check_sensory(out_path)
        except Exception: pass
        label_name_issues: list = []
        try:
            from .label_name_check import check_label_in_name; label_name_issues = check_label_in_name(out_path)
        except Exception: pass
        nontext_contrast_issues: list = []; nontext_fixes_applied: int = 0
        try:
            from .nontext_contrast import check_nontext_contrast
            nontext_contrast_issues = check_nontext_contrast(out_path)
            if nontext_contrast_issues:
                from .contrast_fix import fix_nontext_contrast
                nontext_fixes_applied, _ = fix_nontext_contrast(out_path, nontext_contrast_issues)
                if nontext_fixes_applied: nontext_contrast_issues = check_nontext_contrast(out_path)
        except Exception: pass
        target_size_issues: list = []
        try:
            from .target_size import check_target_size; target_size_issues = check_target_size(out_path)
        except Exception: pass
        xfa_warning = None
        try:
            from .xfa_detect import detect_xfa; xfa_warning = detect_xfa(out_path)
        except Exception: pass
        font_issues: list = []
        try:
            from .font_check import check_fonts; font_issues = check_fonts(out_path)
        except Exception: pass
        reflow_issues: list = []
        try:
            from .reflow_check import check_reflow; reflow_issues = check_reflow(out_path)
        except Exception: pass
        metadata_issues: list = []
        try:
            from .metadata_check import check_metadata; metadata_issues = check_metadata(out_path)
        except Exception: pass
        watermark_candidates: list = []
        try:
            from .watermark_detect import detect_watermarks; watermark_candidates = detect_watermarks(out_path)
        except Exception: pass
        abbrev_list: list = []
        try:
            from .abbrev_detect import detect_abbreviations; abbrev_list = detect_abbreviations(out_path)
        except Exception: pass
        reading_level: dict = {}
        try:
            from .reading_level import assess_reading_level; reading_level = assess_reading_level(out_path)
        except Exception: pass
        link_text_issues: list = []
        try:
            from .link_text_check import check_link_text; link_text_issues = check_link_text(out_path)
        except Exception: pass
        table_structure_issues: list = []
        try:
            from .table_structure_check import check_table_structure; table_structure_issues = check_table_structure(out_path)
        except Exception: pass
        language_issues: list = []
        try:
            from .language_check import check_language; language_issues = check_language(out_path)
        except Exception: pass
        struct_completeness: dict = {}
        try:
            from .struct_complete import check_struct_completeness; struct_completeness = check_struct_completeness(out_path, manifest_obj)
        except Exception: pass
        round_trip: dict = {}
        try:
            from .round_trip_check import run as round_trip_run
            round_trip = round_trip_run(out_path, manifest_obj)
            if round_trip.get("failed", 0) > 0:
                log.warning("REMEDIATE round-trip: %d failure(s)", round_trip["failed"])
        except Exception as _rt_exc:
            log.debug("round_trip_check skipped: %s", _rt_exc)
        verapdf_repairs: int = 0; verapdf_repair_notes: list = []
        if not result.compliant and result.failures:
            try:
                from .verapdf_auto_repair import auto_repair
                verapdf_repairs, verapdf_repair_notes = auto_repair(
                    out_path, [f.__dict__ if hasattr(f,"__dict__") else f for f in result.failures])
                if verapdf_repairs > 0:
                    try: result = validate_pdf(out_path, flavour=flavour)
                    except Exception as _rev: log.warning("re-validation failed: %s", _rev)
            except Exception: pass
        contrast_repairs: int = 0; contrast_repair_notes: list = []
        try:
            from .contrast_fix import fix_contrast; contrast_repairs, contrast_repair_notes = fix_contrast(out_path, contrast_failures)
        except Exception: pass
        fonts_embedded: int = 0; font_embed_notes: list = []
        try:
            from .font_embed import embed_fonts; fonts_embedded, font_embed_notes = embed_fonts(out_path, font_issues)
        except Exception: pass
        # Re-validate after font embedding — fonts are embedded AFTER the initial validate_pdf
        # call, so compliant=False from font issues would be stale without this re-check.
        if fonts_embedded > 0 and not result.compliant:
            try:
                result = validate_pdf(out_path, flavour=flavour)
            except Exception: pass
        # ── AI visual review + auto-fix ───────────────────────────────────────
        # Claude reviews the rendered result and fixes the confident visual
        # mismatches (mis-tagged headings, wrong alt, title) in place; items it
        # can't fix mechanically come back under visual_review["remaining"].
        visual_review: dict = {}
        try:
            from .ai_visual_fix import run_visual_fix
            visual_review = run_visual_fix(out_path)
            if visual_review.get("applied"):
                log.info("REMEDIATE visual-fix applied %d fix(es)", len(visual_review["applied"]))
                try:
                    result = safe_validate_pdf(out_path, flavour=flavour)
                except Exception: pass
        except Exception as _vf_exc:
            log.warning("visual fix skipped: %s", _vf_exc)
        ocg_issues: list = []
        try:
            from .ocg_check import check_optional_content; ocg_issues = check_optional_content(out_path)
        except Exception: pass
        security: dict = {}
        try:
            from .security_check import check_security; security = check_security(out_path)
        except Exception: pass
        # ── Sprint 11: AI composite score ────────────────────────────────────
        _score_manifest = dict(manifest_obj)
        _score_manifest.update({
            "altIssueCount": len(alt_issues),
            "headingIssueCount": len(heading_issues),
            "colorOnlyCount": len(color_only_warnings),
            "sensoryIssueCount": len(sensory_issues),
            "fontIssueCount": len(font_issues),
            "contrastIssueCount": len(contrast_failures),
            "linkTextIssueCount": len(link_text_issues),
            "tableStructureIssueCount": len(table_structure_issues),
            "languageIssueCount": len(language_issues),
            "metadataIssueCount": len(metadata_issues),
        })
        ai_accessibility_score: dict | None = None
        try:
            from .ai_score import score_accessibility
            ai_accessibility_score = score_accessibility(_score_manifest)
        except Exception: pass
        # ── Regression guard: never hand back a file worse than the input ─────
        if baseline_result is not None and result.failed_checks > baseline_result.failed_checks:
            log.warning(
                "REMEDIATE regression guard fired: input=%d output=%d failed_checks — returning original unchanged",
                baseline_result.failed_checks, result.failed_checks)
            import shutil as _shutil
            _shutil.copyfile(in_path, out_path)
            regression_guard = {
                "triggered": True,
                "inputFailedChecks": baseline_result.failed_checks,
                "rejectedFailedChecks": result.failed_checks,
                "note": ("Automated remediation would have increased failing checks; the "
                         "original file is returned unchanged. Manual remediation is recommended."),
            }
            result = baseline_result
        with open(out_path, "rb") as fh: pdf_bytes = fh.read()
        elapsed = time.monotonic() - t0
        log.info("REMEDIATE done  file=%r  elapsed=%.1fs  compliant=%s  regression_guard=%s",
                 file.filename, elapsed, result.compliant, regression_guard["triggered"])
    except WritebackError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except VeraPDFError as exc:
        raise HTTPException(status_code=500, detail=f"Validation failed: {exc}") from exc
    finally:
        for p in (in_path, out_path):
            if os.path.exists(p): os.unlink(p)
        if fixed_path and os.path.exists(fixed_path): os.unlink(fixed_path)
    try:
        from .verapdf_explain import enrich_failures
        enriched_failures = enrich_failures([f.__dict__ if hasattr(f,"__dict__") else f for f in result.failures])
    except Exception:
        enriched_failures = [vars(f) if hasattr(f,"__dict__") else f for f in result.failures]
    base = _safe_filename((file.filename or "document.pdf").rsplit(".", 1)[0])
    src = manifest_obj.get("source", {})
    conformance = {
        "compliant": result.compliant, "flavour": result.flavour,
        "failedRules": result.failed_rules, "failures": enriched_failures, "report": report,
        "contrastFailures": contrast_failures, "contrastCount": len(contrast_failures), "contrastFixes": contrast_fixes,
        "linkQualityIssues": link_quality_issues, "linkQualityCount": len(link_quality_issues),
        "linkQualityFixed": link_quality_auto_fixed, "linkQualityFixedCount": len(link_quality_auto_fixed),
        "backgroundFillsWhitened": report.get("backgroundFillsWhitened", 0),
        "headerFooterArtifacts": src.get("headerFooterArtifacts", 0),
        "readingOrderFixed": src.get("readingOrderFixed", 0), "langAnnotations": src.get("langAnnotations", 0),
        "formTotal": form_total, "formFixed": form_fixed, "formFields": form_fields,
        "altIssues": alt_issues, "altIssueCount": len(alt_issues),
        "colorOnlyWarnings": color_only_warnings, "colorOnlyCount": len(color_only_warnings),
        "headingIssues": heading_issues, "headingIssueCount": len(heading_issues),
        "sensoryIssues": sensory_issues, "sensoryIssueCount": len(sensory_issues),
        "labelNameIssues": label_name_issues, "labelNameIssueCount": len(label_name_issues),
        "footnotePairsWired": report.get("footnote_pairs_wired", 0),
        "tocItemsTagged": src.get("tocItemsTagged", 0), "nestedListsFixed": src.get("nestedListsFixed", 0),
        "nontextContrastIssues": nontext_contrast_issues, "nontextContrastCount": len(nontext_contrast_issues),
        "nontextContrastFixed": nontext_fixes_applied,
        "targetSizeIssues": target_size_issues, "targetSizeCount": len(target_size_issues),
        "xfaWarning": xfa_warning,
        "fontIssues": font_issues, "fontIssueCount": len(font_issues),
        "reflowIssues": reflow_issues, "reflowIssueCount": len(reflow_issues),
        "metadataIssues": metadata_issues, "metadataIssueCount": len(metadata_issues),
        "watermarkCandidates": watermark_candidates, "watermarkCount": len(watermark_candidates),
        "abbreviations": abbrev_list, "abbreviationCount": len(abbrev_list),
        "readingLevel": reading_level,
        "tableSummaries": src.get("aiEnhance", {}).get("table_summaries", 0),
        "altQualityIssues": src.get("altQualityIssues", []),
        "altQualityCount": src.get("aiEnhance", {}).get("alt_flagged", 0),
        "pdfuaMetadata": report.get("pdfuaMetadata", {}),
        "annotContentsFixed": report.get("annotContentsFixed", 0), "annotIssues": report.get("annotIssues", []),
        "radioGroupsFixed": src.get("radioGroupsFixed", 0),
        "structCompleteness": struct_completeness, "roundTrip": round_trip,
        "verapdfRepairs": verapdf_repairs, "verapdfRepairNotes": verapdf_repair_notes,
        "captionsLinked": src.get("captionsLinked", 0), "formulasTagged": src.get("formulasTagged", 0),
        "aiAltGenerated": src.get("aiAltGenerated", 0),
        "contrastRepairs": contrast_repairs, "contrastRepairNotes": contrast_repair_notes,
        "fontsEmbedded": fonts_embedded, "fontEmbedNotes": font_embed_notes,
        "ocgIssues": ocg_issues, "ocgIssueCount": len(ocg_issues), "security": security,
        "linkTextIssues": link_text_issues, "linkTextIssueCount": len(link_text_issues),
        "tableStructureIssues": table_structure_issues, "tableStructureIssueCount": len(table_structure_issues),
        "languageIssues": language_issues, "languageIssueCount": len(language_issues),
        "aiAccessibilityScore": ai_accessibility_score,
        "regressionGuard": regression_guard,
        "remediationMode": report.get("mode", "rebuild"),
        "validationError": result.validation_error,
        "validationComplete": result.validation_error is None,
        "visualReview": visual_review,
    }
    headers = {
        "Content-Disposition": f'attachment; filename="{base}.remediated.pdf"',
        "X-Conformance": json.dumps(conformance),
        "X-VeraPDF-Compliant": str(result.compliant).lower(),
        "X-VeraPDF-Failed-Rules": str(result.failed_rules),
        "X-Remediation-Elements": str(report.get("elements", 0)),
        "X-Remediation-MCIDs": str(report.get("mcids", 0)),
    }
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)

@app.post("/quickfix", tags=["patch"], summary="Quick Fix All — apply every AI-driven auto-fix to an already-remediated PDF in one pass")
def quickfix(file: UploadFile = File(...)) -> Response:
    """Runs all fixers in sequence: contrast, heading levels, table scope,
    metadata, font embedding, language tagging, alt text, veraPDF repair.
    Returns the patched PDF with an X-QuickFix-Result JSON header."""
    in_path = _save_upload(file)
    try:
        from .quickfix import run_quickfix
        result = run_quickfix(in_path)
        with open(in_path, "rb") as fh:
            pdf_bytes = fh.read()
    finally:
        if os.path.exists(in_path):
            os.unlink(in_path)
    base = _safe_filename((file.filename or "document.pdf").rsplit(".", 1)[0])
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{base}.quickfix.pdf"',
            "X-QuickFix-Result": json.dumps(result),
        },
    )

@app.post("/patch", tags=["patch"], summary="Apply a one-click fix to an already-remediated PDF (metadata | headings | tables)")
def patch(
    file: UploadFile = File(...),
    action: str = Form(...),
    title: str = Form(""), lang: str = Form(""),
    author: str = Form(""), subject: str = Form(""),
) -> Response:
    from .patch_pdf import patch_metadata, patch_heading_levels, patch_table_headers
    KNOWN_ACTIONS = {"metadata", "headings", "tables"}
    if action not in KNOWN_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown action {action!r}.")
    in_path = _save_upload(file)
    try:
        if action == "metadata":
            result = patch_metadata(in_path, title=title, lang=lang, author=author, subject=subject)
        elif action == "tables":
            result = patch_table_headers(in_path)
        else:
            result = patch_heading_levels(in_path)
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result.get("error", "Patch failed"))
        with open(in_path, "rb") as fh: pdf_bytes = fh.read()
    finally:
        if os.path.exists(in_path): os.unlink(in_path)
    base = _safe_filename((file.filename or "document.pdf").rsplit(".", 1)[0])
    return Response(content=pdf_bytes, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{base}.patched.pdf"',
        "X-Patch-Result": json.dumps(result),
    })

@app.post("/visual-check", tags=["core"], summary="AI visual review of the human-judgment checkpoints (alt text accuracy, reading order, headings, decorative choices)")
def visual_check(file: UploadFile = File(...), max_pages: int = Form(6)) -> JSONResponse:
    """Render the (remediated) PDF's pages and have Claude review the judgment
    areas automated validators can't verify — whether alt text matches the
    images, whether the reading order is visually sensible, whether headings
    and decorative choices look right. Returns per-item verdicts that triage
    what a human reviewer should confirm. Assistive only: it never replaces
    human verification or changes the conformance result."""
    in_path = _save_upload(file)
    try:
        from .ai_visual_check import run_visual_check
        result = run_visual_check(in_path, max_pages=max(1, min(int(max_pages), 12)))
    finally:
        if os.path.exists(in_path):
            os.unlink(in_path)
    return JSONResponse(result)


@app.post("/reading-order", tags=["reorder"], summary="Extract the current reading order from a PDF's structure tree")
def reading_order_get(file: UploadFile = File(...)) -> JSONResponse:
    in_path = _save_upload(file)
    try:
        from .reading_order import extract_reading_order
        elements = extract_reading_order(in_path)
    finally:
        if os.path.exists(in_path): os.unlink(in_path)
    return JSONResponse({"elements": elements, "count": len(elements)})

@app.post("/reorder", tags=["reorder"], summary="Rewrite a PDF's structure tree to match a specified element order")
def reorder(file: UploadFile = File(...), order: str = Form(...)) -> Response:
    try:
        ordered_ids: list[str] = json.loads(order)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="order must be a JSON array of element IDs")
    in_path = _save_upload(file)
    try:
        from .reading_order import apply_reading_order
        result = apply_reading_order(in_path, ordered_ids)
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result.get("error", "Reorder failed"))
        with open(in_path, "rb") as fh: pdf_bytes = fh.read()
    finally:
        if os.path.exists(in_path): os.unlink(in_path)
    base = _safe_filename((file.filename or "document.pdf").rsplit(".", 1)[0])
    return Response(content=pdf_bytes, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{base}.reordered.pdf"',
        "X-Reorder-Result": json.dumps(result),
    })

@app.post("/audit-report", tags=["core"], summary="Generate a structured accessibility audit report for a PDF")
async def audit_report_endpoint(request: Request) -> Response:
    try:
        request_body = await request.json()
        from .audit_report import generate_report
        filename = _safe_filename(request_body.get("filename", "document.pdf"))
        html_str = generate_report(filename, request_body)
        return Response(content=html_str.encode("utf-8"), media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}.audit.html"'})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

@app.post("/batch", tags=["batch"], summary="Remediate multiple PDFs in one request — returns a ZIP of accessible PDFs")
async def batch_remediate(files: list[UploadFile] = File(...), flavour: str = Form(DEFAULT_FLAVOUR)) -> JSONResponse:
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(status_code=400, detail=f"Batch limited to {MAX_BATCH_FILES} files per request.")
    results = []
    for upload in files:
        path = _save_upload(upload)
        try:
            manifest_obj = autotag_pdf(path, detect_headers=True)
            manifest_obj.pop("_questions", None)
            fd, out_path = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
            remediate_pdf(path, manifest_obj, out_path)
            result = validate_pdf(out_path, flavour=flavour)
            import base64
            with open(out_path, "rb") as fh:
                pdf_b64 = base64.b64encode(fh.read()).decode()
            results.append({
                "filename": upload.filename or "document.pdf",
                "ok": True,
                "compliant": result.compliant,
                "failedRules": result.failed_rules,
                "pdf": pdf_b64,
            })
        except Exception as exc:
            results.append({"filename": upload.filename or "document.pdf", "ok": False, "error": str(exc)})
        finally:
            try: os.unlink(path)
            except OSError: pass
    return JSONResponse(results)
