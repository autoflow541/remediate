"""FastAPI app for the PDF accessibility remediation engine.

Phase 1 ships POST /validate (veraPDF). /autotag and /remediate are wired as
stubs so the API surface is stable and the studio can target real URLs that
return clear 501s until those phases land.

Stateless by design: every uploaded PDF is written to a temp file, processed,
and deleted in a finally block. Nothing is persisted.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
import json

from .log_config import configure as _configure_logging
_configure_logging()
log = logging.getLogger(__name__)

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from .autotag import AutotagError, autotag_pdf
from .manifest import count_nodes
from .validate import (
    DEFAULT_FLAVOUR,
    KNOWN_FLAVOURS,
    VeraPDFError,
    get_verapdf_version,
    validate_pdf,
)
from .writeback import WritebackError, remediate_pdf

app = FastAPI(
    title="PDF Accessibility Remediation Engine",
    version="0.1.0",
    description="Auto-tag, write-back, and validate PDFs to PDF/UA. Free and open source.",
)

# The studio runs entirely in the browser and calls this service cross-origin.
# Allow origins from env (comma-separated) or default to permissive for local dev.
_origins = os.environ.get("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    # The studio reads the conformance result off these custom headers, so they
    # must be exposed to browser JS (not exposed by default under CORS).
    expose_headers=[
        "X-Conformance",
        "X-VeraPDF-Compliant",
        "X-VeraPDF-Failed-Rules",
        "X-Remediation-Elements",
        "X-Remediation-MCIDs",
        "Content-Disposition",
    ],
)

# ---------------------------------------------------------------------------
# Request logging middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def _log_requests(request: Request, call_next):
    """Log every request: method, path, status, and wall-clock time."""
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000
    log.info(
        "%s %s -> %d  (%.0f ms)",
        request.method, request.url.path,
        response.status_code, ms,
    )
    return response


# Reject absurdly large uploads early (bytes). 100 MB default.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))

# Maximum files accepted by /batch in a single request.
MAX_BATCH_FILES = int(os.environ.get("MAX_BATCH_FILES", "20"))


def _safe_filename(name: str) -> str:
    """Sanitize a user-supplied filename for use in a Content-Disposition header.

    Strips characters that could break the quoted-string parameter value
    (double-quote, backslash, CR, LF) and truncates to 200 chars to prevent
    oversized headers.
    """
    name = re.sub(r'[\r\n"\\]', '_', name)
    return name[:200]


def _save_upload(upload: UploadFile) -> str:
    """Persist an UploadFile to a temp .pdf and return its path.

    Enforces the size cap while streaming so we never buffer an oversized file
    fully in memory.
    """
    fd, path = tempfile.mkstemp(suffix=".pdf")
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds {MAX_UPLOAD_BYTES} byte limit.",
                    )
                out.write(chunk)
    except Exception:
        # Clean up the partial temp file on any error before re-raising.
        if os.path.exists(path):
            os.unlink(path)
        raise
    return path


@app.get("/health")
def health() -> dict:
    """Liveness + dependency probe.

    Reports whether veraPDF (and thus the JRE) is reachable. This is the first
    thing to check when bringing the container up — it proves the Java plumbing.
    """
    version = get_verapdf_version()
    return {
        "status": "ok",
        "verapdf": version,
        "verapdf_available": version is not None,
    }



@app.post("/validate")
def validate(
    file: UploadFile = File(...),
    flavour: str = Form(DEFAULT_FLAVOUR),
) -> JSONResponse:
    """Validate an uploaded PDF against a PDF/UA (or PDF/A) flavour.

    Returns the veraPDF conformance report: overall pass/fail plus every failed
    clause with its on-page context. Useful standalone as a free PDF/UA checker.
    """
    if flavour not in KNOWN_FLAVOURS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown flavour {flavour!r}. Known: {sorted(KNOWN_FLAVOURS)}",
        )

    path = _save_upload(file)
    try:
        result = validate_pdf(path, flavour=flavour)
    except VeraPDFError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if os.path.exists(path):
            os.unlink(path)

    return JSONResponse(result.to_dict())


@app.post("/autotag")
def autotag(
    file: UploadFile = File(...),
    detect_headers: bool = Form(True),
) -> JSONResponse:
    """Auto-tag an uploaded PDF into a draft remediation manifest.

    Runs OpenDataLoader layout analysis and returns a structure-tree manifest
    (headings, paragraphs, tables, figures, reading order) for the studio to
    refine. The manifest is a draft: alt text, title and language are left for
    the human to supply. With ``detect_headers`` (default on), table header cells
    are proposed (first row -> column headers) for the human to confirm.
    """
    path = _save_upload(file)
    try:
        manifest = autotag_pdf(path, detect_headers=detect_headers)
    except AutotagError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if os.path.exists(path):
            os.unlink(path)

    # Record the uploaded name, not the server-side temp path.
    if file.filename:
        manifest["source"]["filename"] = file.filename
    manifest["source"]["nodeCount"] = count_nodes(manifest["nodes"])
    return JSONResponse(manifest)


@app.post("/remediate")
def remediate(
    file: UploadFile = File(...),
    manifest: UploadFile = File(...),
    flavour: str = Form(DEFAULT_FLAVOUR),
) -> Response:
    """Fuse a PDF with the studio's manifest and return a tagged PDF/UA file.

    Writes a real structure tree (headings, paragraphs, tables, figures with alt
    text, reading order), sets language/title and the PDF/UA claim, then validates
    the result with veraPDF. The fixed PDF is returned as the response body; the
    write-back summary and conformance result are returned in ``X-*`` headers
    (and a compact JSON copy in ``X-Conformance``) so the studio can update its
    ledger without a second round-trip.
    """
    try:
        manifest_obj = json.loads(manifest.file.read())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid manifest JSON: {exc}")
    if not isinstance(manifest_obj, dict):
        raise HTTPException(status_code=400, detail="Manifest must be a JSON object.")

    in_path = _save_upload(file)
    fd, out_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    fd2, fixed_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd2)
    contrast_fixes = 0
    t0 = time.monotonic()
    log.info("REMEDIATE start  file=%r", file.filename)
    try:
        report = remediate_pdf(in_path, manifest_obj, out_path)

        # Step 1: strip decorative colored background fills before contrast check.
        try:
            from .clean_background import clean_background_fills
            fd3, bg_path = tempfile.mkstemp(suffix=".pdf")
            os.close(fd3)
            bg_fixes = clean_background_fills(out_path, bg_path)
            if bg_fixes > 0:
                os.replace(bg_path, out_path)
                report["backgroundFillsWhitened"] = bg_fixes
                log.info("BACKGROUND whitened %d fills  file=%r", bg_fixes, file.filename)
            else:
                os.unlink(bg_path)
                report["backgroundFillsWhitened"] = 0
        except Exception as _bg_exc:
            log.warning("Background cleanup skipped: %s", _bg_exc)
            report["backgroundFillsWhitened"] = 0

        # Step 2: fix contrast failures by recoloring text (WCAG 1.4.3)
        try:
            from .fix_contrast import fix_contrast_colors
            contrast_fixes = fix_contrast_colors(out_path, fixed_path)
            if contrast_fixes > 0:
                os.replace(fixed_path, out_path)
                fixed_path = None
            else:
                os.unlink(fixed_path)
                fixed_path = None
        except Exception:
            pass  # Never block remediation due to contrast fix failure

        # Step 3: form field remediation (WCAG 4.1.2)
        form_total = 0
        form_fixed = 0
        form_fields: list = []
        try:
            from .form_fields import remediate_form_fields
            form_total, form_fixed, form_fields = remediate_form_fields(out_path)
        except Exception:
            pass

        result = validate_pdf(out_path, flavour=flavour)

        # Step 4: verify contrast on final PDF
        contrast_failures: list = []
        try:
            from .contrast import check_contrast
            contrast_failures = check_contrast(out_path)
        except Exception:
            pass

        # Step 5: link quality check (WCAG 2.4.4)
        link_quality_issues: list = []
        link_quality_auto_fixed: list = []
        try:
            from .link_quality import check_link_quality
            raw_link_issues = check_link_quality(out_path)

            from .fix_link_text import generate_link_description
            for issue in raw_link_issues:
                url = issue.get("url", "")
                if url:
                    desc = generate_link_description(url, issue.get("text", ""))
                    link_quality_auto_fixed.append({**issue, "autoFixedAlt": desc})
                else:
                    link_quality_issues.append(issue)
        except Exception:
            pass

        # Step 6: alt text quality check (WCAG 1.1.1)
        alt_issues: list = []
        try:
            from .alt_quality import check_alt_quality
            alt_issues = check_alt_quality(out_path)
        except Exception:
            pass

        # Step 7: color-only information detection (WCAG 1.4.1)
        color_only_warnings: list = []
        try:
            from .color_only import detect_color_only
            color_only_warnings = detect_color_only(out_path)
        except Exception:
            pass

        # Step 8: heading hierarchy validation (WCAG 1.3.1 / 2.4.6)
        heading_issues: list = []
        try:
            from .heading_check import check_headings
            heading_issues = check_headings(out_path)
        except Exception:
            pass

        # Step 9: sensory-characteristics check (WCAG 1.3.3)
        sensory_issues: list = []
        try:
            from .sensory_check import check_sensory
            sensory_issues = check_sensory(out_path)
        except Exception:
            pass

        # Step 10: WCAG 2.5.3 Label in Name
        label_name_issues: list = []
        try:
            from .label_name_check import check_label_in_name
            label_name_issues = check_label_in_name(out_path)
        except Exception:
            pass

        # Step 11: WCAG 1.4.11 Non-text contrast
        nontext_contrast_issues: list = []
        try:
            from .nontext_contrast import check_nontext_contrast
            nontext_contrast_issues = check_nontext_contrast(out_path)
        except Exception:
            pass

        # Step 12: WCAG 2.5.8 Target size
        target_size_issues: list = []
        try:
            from .target_size import check_target_size
            target_size_issues = check_target_size(out_path)
        except Exception:
            pass

        # Step 13: XFA form detection
        xfa_warning: dict | None = None
        try:
            from .xfa_detect import detect_xfa
            xfa_warning = detect_xfa(out_path)
        except Exception:
            pass

        # Step 14: Font embedding + ToUnicode
        font_issues: list = []
        try:
            from .font_check import check_fonts
            font_issues = check_fonts(out_path)
        except Exception:
            pass

        # Step 15: Reflow + text spacing
        reflow_issues: list = []
        try:
            from .reflow_check import check_reflow
            reflow_issues = check_reflow(out_path)
        except Exception:
            pass

        # Step 16: Metadata completeness
        metadata_issues: list = []
        try:
            from .metadata_check import check_metadata
            metadata_issues = check_metadata(out_path)
        except Exception:
            pass

        # Step 17: Watermark / background detection
        watermark_candidates: list = []
        try:
            from .watermark_detect import detect_watermarks
            watermark_candidates = detect_watermarks(out_path)
        except Exception:
            pass

        # Step 18: Abbreviation detection
        abbrev_list: list = []
        try:
            from .abbrev_detect import detect_abbreviations
            abbrev_list = detect_abbreviations(out_path)
        except Exception:
            pass

        # Step 19: Reading level
        reading_level: dict = {}
        try:
            from .reading_level import assess_reading_level
            reading_level = assess_reading_level(out_path)
        except Exception:
            pass

        # Step 20: Structure completeness check
        struct_completeness: dict = {}
        try:
            from .struct_complete import check_struct_completeness
            struct_completeness = check_struct_completeness(out_path, manifest_obj)
        except Exception:
            pass

        # Step 20b: Round-trip check — verify output PDF matches manifest intent
        round_trip: dict = {}
        try:
            from .round_trip_check import run as round_trip_run
            round_trip = round_trip_run(out_path, manifest_obj)
            if round_trip.get("failed", 0) > 0:
                log.warning(
                    "REMEDIATE round-trip: %d failure(s): %s",
                    round_trip["failed"],
                    [f["check"] for f in round_trip.get("failures", [])],
                )
        except Exception as _rt_exc:
            log.debug("round_trip_check skipped: %s", _rt_exc)

        # Step 21: veraPDF targeted auto-repair + re-validate
        verapdf_repairs: int = 0
        verapdf_repair_notes: list = []
        if not result.compliant and result.failures:
            try:
                from .verapdf_auto_repair import auto_repair
                verapdf_repairs, verapdf_repair_notes = auto_repair(
                    out_path,
                    [f.__dict__ if hasattr(f, "__dict__") else f for f in result.failures],
                )
                # Re-run veraPDF so result.compliant reflects the post-repair state.
                # Without this, a PDF that passes after repair still shows as non-compliant.
                if verapdf_repairs > 0:
                    try:
                        result = validate_pdf(out_path, flavour=flavour)
                        log.info(
                            "REMEDIATE re-validated after %d repair(s): compliant=%s",
                            verapdf_repairs, result.compliant,
                        )
                    except Exception as _rev_exc:
                        log.warning("REMEDIATE re-validation failed: %s", _rev_exc)
            except Exception:
                pass

        # Step 22: Contrast auto-fix
        contrast_repairs: int = 0
        contrast_repair_notes: list = []
        try:
            from .contrast_fix import fix_contrast
            contrast_repairs, contrast_repair_notes = fix_contrast(out_path, contrast_failures)
        except Exception:
            pass

        # Step 23: Font embedding auto-repair
        fonts_embedded: int = 0
        font_embed_notes: list = []
        try:
            from .font_embed import embed_fonts
            fonts_embedded, font_embed_notes = embed_fonts(out_path, font_issues)
        except Exception:
            pass

        # Step 24: Optional content / layer accessibility check
        ocg_issues: list = []
        try:
            from .ocg_check import check_optional_content
            ocg_issues = check_optional_content(out_path)
        except Exception:
            pass

        # Step 25: Encryption / security permissions check
        security: dict = {}
        try:
            from .security_check import check_security
            security = check_security(out_path)
        except Exception:
            pass

        with open(out_path, "rb") as fh:
            pdf_bytes = fh.read()

        elapsed = time.monotonic() - t0
        log.info(
            "REMEDIATE done   file=%r  elapsed=%.1fs  compliant=%s  "
            "elements=%d  bookmarks=%d  figures=%d  artifacts=%d  "
            "footnote_pairs=%d  verapdf_repairs=%d",
            file.filename, elapsed, result.compliant,
            report.get("elements", 0), report.get("bookmarks", 0),
            report.get("figures", 0), report.get("artifacts_decorative", 0),
            report.get("footnote_pairs_wired", 0), verapdf_repairs,
        )
    except WritebackError as exc:
        log.error("REMEDIATE failed  file=%r  error=%s", file.filename, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except VeraPDFError as exc:
        log.error("REMEDIATE failed  file=%r  error=%s", file.filename, exc)
        raise HTTPException(status_code=500, detail=f"Validation failed: {exc}") from exc
    finally:
        for p in (in_path, out_path):
            if os.path.exists(p):
                os.unlink(p)
        if fixed_path and os.path.exists(fixed_path):
            os.unlink(fixed_path)

    try:
        from .verapdf_explain import enrich_failures
        enriched_failures = enrich_failures([f.__dict__ if hasattr(f, '__dict__') else f
                                             for f in result.failures])
    except Exception:
        enriched_failures = [vars(f) if hasattr(f, '__dict__') else f for f in result.failures]

    # SECURITY: sanitize filename before embedding in response header to prevent
    # header injection via crafted filenames containing CR/LF/quotes.
    base = _safe_filename((file.filename or "document.pdf").rsplit(".", 1)[0])
    src = manifest_obj.get("source", {})
    conformance = {
        "compliant": result.compliant,
        "flavour": result.flavour,
        "failedRules": result.failed_rules,
        "failures": enriched_failures,
        "report": report,
        "contrastFailures": contrast_failures,
        "contrastCount": len(contrast_failures),
        "contrastFixes": contrast_fixes,
        "linkQualityIssues": link_quality_issues,
        "linkQualityCount": len(link_quality_issues),
        "linkQualityFixed": link_quality_auto_fixed,
        "linkQualityFixedCount": len(link_quality_auto_fixed),
        "backgroundFillsWhitened": report.get("backgroundFillsWhitened", 0),
        "headerFooterArtifacts": src.get("headerFooterArtifacts", 0),
        "readingOrderFixed": src.get("readingOrderFixed", 0),
        "langAnnotations": src.get("langAnnotations", 0),
        "formTotal": form_total,
        "formFixed": form_fixed,
        "formFields": form_fields,
        "altIssues": alt_issues,
        "altIssueCount": len(alt_issues),
        "colorOnlyWarnings": color_only_warnings,
        "colorOnlyCount": len(color_only_warnings),
        "headingIssues": heading_issues,
        "headingIssueCount": len(heading_issues),
        "sensoryIssues": sensory_issues,
        "sensoryIssueCount": len(sensory_issues),
        "labelNameIssues": label_name_issues,
        "labelNameIssueCount": len(label_name_issues),
        "footnotePairsWired": report.get("footnote_pairs_wired", 0),
        "tocItemsTagged": src.get("tocItemsTagged", 0),
        "nestedListsFixed": src.get("nestedListsFixed", 0),
        "nontextContrastIssues": nontext_contrast_issues,
        "nontextContrastCount": len(nontext_contrast_issues),
        "targetSizeIssues": target_size_issues,
        "targetSizeCount": len(target_size_issues),
        "xfaWarning": xfa_warning,
        "fontIssues": font_issues,
        "fontIssueCount": len(font_issues),
        "reflowIssues": reflow_issues,
        "reflowIssueCount": len(reflow_issues),
        "metadataIssues": metadata_issues,
        "metadataIssueCount": len(metadata_issues),
        "watermarkCandidates": watermark_candidates,
        "watermarkCount": len(watermark_candidates),
        "abbreviations": abbrev_list,
        "abbreviationCount": len(abbrev_list),
        "readingLevel": reading_level,
        "tableSummaries": src.get("aiEnhance", {}).get("table_summaries", 0),
        "altQualityIssues": src.get("altQualityIssues", []),
        "altQualityCount": src.get("aiEnhance", {}).get("alt_flagged", 0),
        "pdfuaMetadata": report.get("pdfuaMetadata", {}),
        "annotContentsFixed": report.get("annotContentsFixed", 0),
        "annotIssues": report.get("annotIssues", []),
        "radioGroupsFixed": src.get("radioGroupsFixed", 0),
        "structCompleteness": struct_completeness,
        "roundTrip": round_trip,
        "verapdfRepairs": verapdf_repairs,
        "verapdfRepairNotes": verapdf_repair_notes,
        # Sprint 19-25
        "captionsLinked": src.get("captionsLinked", 0),
        "formulasTagged": src.get("formulasTagged", 0),
        "aiAltGenerated": src.get("aiAltGenerated", 0),
        "contrastRepairs": contrast_repairs,
        "contrastRepairNotes": contrast_repair_notes,
        "fontsEmbedded": fonts_embedded,
        "fontEmbedNotes": font_embed_notes,
        "ocgIssues": ocg_issues,
        "ocgIssueCount": len(ocg_issues),
        "security": security,
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


@app.post("/audit-report")
async def audit_report_endpoint(request: Request) -> Response:
    """Generate an HTML accessibility audit report from a conformance dict.

    POST the JSON conformance object (from X-Conformance header) and receive
    a self-contained HTML report suitable for accessibility coordinators.
    """
    try:
        request_body = await request.json()
        from .audit_report import generate_report
        # SECURITY: sanitize filename to prevent header injection.
        filename = _safe_filename(request_body.get("filename", "document.pdf"))
        html_str = generate_report(filename, request_body)
        return Response(
            content=html_str.encode("utf-8"),
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}.audit.html"'},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/batch")
async def batch_remediate(
    files: list[UploadFile] = File(...),
    flavour: str = Form(DEFAULT_FLAVOUR),
) -> JSONResponse:
    """Remediate multiple PDFs using their embedded manifests (if any).

    Each file is auto-tagged and immediately remediated with default settings.
    Returns a JSON list of per-file conformance summaries.  Large files or
    files with complex manifests should use /remediate individually.

    Note: batch mode skips the human manifest-review step — it applies
    automatic tagging only.  Use for bulk pre-screening, not final output.
    """
    # SECURITY: cap number of files to prevent DoS via unlimited batch requests.
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Batch limited to {MAX_BATCH_FILES} files per request.",
        )

    results = []
    for upload in files:
        in_path = None
        out_path = None
        try:
            in_path = _save_upload(upload)
            fd, out_path = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)

            # Auto-tag
            try:
                manifest_obj = autotag_pdf(in_path)
            except AutotagError as exc:
                results.append({"filename": upload.filename, "error": str(exc)})
                continue

            if upload.filename:
                manifest_obj.setdefault("source", {})["filename"] = upload.filename

            # Remediate
            try:
                report = remediate_pdf(in_path, manifest_obj, out_path)
            except WritebackError as exc:
                results.append({"filename": upload.filename, "error": str(exc)})
                continue

            # Validate
            try:
                vresult = validate_pdf(out_path, flavour=flavour)
            except VeraPDFError as exc:
                results.append({"filename": upload.filename, "error": str(exc)})
                continue

            results.append({
                "filename": upload.filename,
                "compliant": vresult.compliant,
                "failedRules": vresult.failed_rules,
                "elements": report.get("elements", 0),
                "mcids": report.get("mcids", 0),
            })
        finally:
            for p in (in_path, out_path):
                if p and os.path.exists(p):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

    return JSONResponse({"results": results, "count": len(results)})
