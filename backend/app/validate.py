"""veraPDF subprocess wrapper.

Runs the veraPDF CLI (the industry-standard PDF/UA + PDF/A checker) as a
subprocess and parses its machine-readable XML report into a structured,
JSON-serializable result. This feeds the studio's conformance ledger:
pass/fail per clause, with the failing checks and their on-page context.

veraPDF is invoked with the *raw* (MRR) XML report format, which every
veraPDF release supports, rather than `--format json` (newer only). We parse
the XML with the stdlib so the wrapper has no extra Python dependency.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from typing import Any


class VeraPDFError(RuntimeError):
    """veraPDF could not be located, or it failed for a non-validation reason.

    A PDF that simply does *not* conform is NOT an error — that is a normal,
    successful run that reports ``compliant: false``. This exception is for
    plumbing failures: missing executable, crash, malformed report, timeout.
    """


# veraPDF flavour ids accepted on the CLI. We expose the common ones; the
# default for this tool is PDF/UA-1, the accessibility flavour.
KNOWN_FLAVOURS = {
    "ua1": "PDF/UA-1",
    "ua2": "PDF/UA-2",
    "1a": "PDF/A-1A",
    "1b": "PDF/A-1B",
    "2a": "PDF/A-2A",
    "2b": "PDF/A-2B",
    "2u": "PDF/A-2U",
    "3a": "PDF/A-3A",
    "3b": "PDF/A-3B",
    "3u": "PDF/A-3U",
}

DEFAULT_FLAVOUR = "ua1"
DEFAULT_TIMEOUT = 180  # seconds; veraPDF is fast but large/broken PDFs can stall


@dataclass
class RuleResult:
    """One PDF/UA (or PDF/A) clause evaluated against the document."""

    specification: str
    clause: str
    test_number: int
    status: str  # "passed" | "failed"
    description: str
    passed_checks: int
    failed_checks: int
    # Up to a handful of failing-check contexts (XPath-ish locators into the
    # PDF object/content tree). Capped so a document with thousands of failures
    # does not produce a giant report.
    contexts: list[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    compliant: bool
    flavour: str
    passed_rules: int
    failed_rules: int
    passed_checks: int
    failed_checks: int
    # Only the failed rules, sorted by clause — that is what the ledger acts on.
    failures: list[RuleResult] = field(default_factory=list)
    verapdf_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def find_verapdf() -> str:
    """Locate the veraPDF CLI executable.

    Resolution order:
      1. ``VERAPDF_PATH`` env var (explicit override, used in Docker).
      2. ``verapdf`` on PATH (Linux/macOS install) or ``verapdf.bat`` (Windows).

    Raises ``VeraPDFError`` if none is found.
    """
    override = os.environ.get("VERAPDF_PATH")
    if override:
        if os.path.isfile(override):
            return override
        raise VeraPDFError(f"VERAPDF_PATH is set but not a file: {override}")

    for name in ("verapdf", "verapdf.bat", "verapdf.cmd"):
        found = shutil.which(name)
        if found:
            return found

    raise VeraPDFError(
        "veraPDF executable not found. Set VERAPDF_PATH or put 'verapdf' on PATH. "
        "In the provided Docker image it lives at /opt/verapdf/verapdf."
    )


def _parse_report(xml_text: str, requested_flavour: str) -> ValidationResult:
    """Parse a veraPDF raw/MRR XML report into a ValidationResult.

    The report shape (namespaces stripped here for brevity) is::

        <report>
          <buildInformation><releaseDetails ... version="1.26.2"/></buildInformation>
          <jobs>
            <job>
              <validationReport flavour="PDFUA_1" isCompliant="false">
                <details passedRules="80" failedRules="3"
                         passedChecks="900" failedChecks="12">
                  <rule specification="PDF/UA-1" clause="7.1" testNumber="1"
                        status="failed" passedChecks="0" failedChecks="3">
                    <description>...</description>
                    <check status="failed"><context>root/...</context></check>
                  </rule>
                </details>
              </validationReport>
            </job>
          </jobs>
        </report>

    Older veraPDF releases use ``<validationResult>`` instead of
    ``<validationReport>`` and may namespace elements; we match on local tag
    names to stay version-tolerant.
    """

    def local(tag: str) -> str:
        # Strip any "{namespace}" prefix ElementTree prepends.
        return tag.rsplit("}", 1)[-1]

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:  # pragma: no cover - defensive
        raise VeraPDFError(f"Could not parse veraPDF XML report: {exc}") from exc

    # Walk by local tag name so namespaced reports still match.
    by_tag: dict[str, list[ET.Element]] = {}
    for el in root.iter():
        by_tag.setdefault(local(el.tag), []).append(el)

    version = None
    for rd in by_tag.get("releaseDetails", []):
        if rd.get("id") in (None, "core") and rd.get("version"):
            version = rd.get("version")
            break

    result_el = None
    for name in ("validationReport", "validationResult"):
        if by_tag.get(name):
            result_el = by_tag[name][0]
            break

    if result_el is None:
        # No validation block: veraPDF ran but produced nothing parseable.
        raise VeraPDFError(
            "veraPDF report contained no validationReport element. "
            "The file may be encrypted or not a PDF."
        )

    flavour_attr = result_el.get("flavour") or KNOWN_FLAVOURS.get(
        requested_flavour, requested_flavour
    )
    is_compliant = (result_el.get("isCompliant", "false").lower() == "true")

    # details element holds the aggregate counts and the per-rule list.
    details = None
    for child in result_el.iter():
        if local(child.tag) == "details":
            details = child
            break

    passed_rules = failed_rules = passed_checks = failed_checks = 0
    failures: list[RuleResult] = []

    if details is not None:
        passed_rules = int(details.get("passedRules", 0) or 0)
        failed_rules = int(details.get("failedRules", 0) or 0)
        passed_checks = int(details.get("passedChecks", 0) or 0)
        failed_checks = int(details.get("failedChecks", 0) or 0)

        for rule in details.iter():
            if local(rule.tag) != "rule":
                continue
            if rule.get("status", "").lower() != "failed":
                continue

            description = ""
            contexts: list[str] = []
            for sub in rule.iter():
                lt = local(sub.tag)
                if lt == "description" and sub.text:
                    description = sub.text.strip()
                elif lt == "context" and sub.text and len(contexts) < 5:
                    contexts.append(sub.text.strip())

            failures.append(
                RuleResult(
                    specification=rule.get("specification", flavour_attr),
                    clause=rule.get("clause", "?"),
                    test_number=int(rule.get("testNumber", 0) or 0),
                    status="failed",
                    description=description,
                    passed_checks=int(rule.get("passedChecks", 0) or 0),
                    failed_checks=int(rule.get("failedChecks", 0) or 0),
                    contexts=contexts,
                )
            )

    failures.sort(key=lambda r: (r.clause, r.test_number))

    return ValidationResult(
        compliant=is_compliant,
        flavour=flavour_attr,
        passed_rules=passed_rules,
        failed_rules=failed_rules,
        passed_checks=passed_checks,
        failed_checks=failed_checks,
        failures=failures,
        verapdf_version=version,
    )


def validate_pdf(
    pdf_path: str,
    flavour: str = DEFAULT_FLAVOUR,
    timeout: int = DEFAULT_TIMEOUT,
    verapdf_path: str | None = None,
) -> ValidationResult:
    """Validate ``pdf_path`` against a PDF flavour using veraPDF.

    Returns a ValidationResult for both conforming and non-conforming files.
    Raises ``VeraPDFError`` only on genuine plumbing failures.
    """
    if flavour not in KNOWN_FLAVOURS:
        raise VeraPDFError(
            f"Unknown flavour {flavour!r}. Known: {', '.join(sorted(KNOWN_FLAVOURS))}"
        )
    if not os.path.isfile(pdf_path):
        raise VeraPDFError(f"PDF not found: {pdf_path}")

    exe = verapdf_path or find_verapdf()

    # --format xml  -> raw machine-readable report (universally supported)
    # --flavour     -> the conformance level to test against
    # -v / --verbose pulls failing-check contexts into the report
    cmd = [exe, "--format", "xml", "--flavour", flavour, pdf_path]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise VeraPDFError(f"Could not execute veraPDF at {exe!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VeraPDFError(f"veraPDF timed out after {timeout}s on {pdf_path}") from exc

    # veraPDF exit codes:
    #   0 = ran, file is compliant
    #   1 = ran, file is NOT compliant   <- still a successful run for us
    #   >1 = real failure (bad args, parse error, crash)
    if proc.returncode not in (0, 1):
        raise VeraPDFError(
            f"veraPDF exited {proc.returncode}.\n"
            f"stderr: {proc.stderr.strip()[:2000]}\n"
            f"stdout: {proc.stdout.strip()[:500]}"
        )

    if not proc.stdout.strip():
        raise VeraPDFError(
            f"veraPDF produced no report (exit {proc.returncode}). "
            f"stderr: {proc.stderr.strip()[:2000]}"
        )

    return _parse_report(proc.stdout, flavour)


def get_verapdf_version(verapdf_path: str | None = None) -> str | None:
    """Return the veraPDF version string, or None if it can't be determined.

    Used by the health endpoint to prove the Java/veraPDF plumbing is live.
    """
    try:
        exe = verapdf_path or find_verapdf()
    except VeraPDFError:
        return None
    try:
        proc = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    return out or None


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import json
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m app.validate <file.pdf> [flavour]", file=sys.stderr)
        raise SystemExit(2)
    fl = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_FLAVOUR
    res = validate_pdf(sys.argv[1], flavour=fl)
    print(json.dumps(res.to_dict(), indent=2))
