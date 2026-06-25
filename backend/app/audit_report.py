"""HTML accessibility audit report generator — Sprint 27.

Converts the conformance dict from /remediate into a self-contained HTML
report suitable for accessibility coordinators and procurement officers:
plain-language summaries, pass/fail indicators, WCAG references, and a
structured findings table.

Usage:
    from .audit_report import generate_report
    html_str = generate_report(filename, conformance_dict)
"""

from __future__ import annotations

import html as _html
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _e(s) -> str:
    return _html.escape(str(s) if s is not None else "")


def _badge(text: str, kind: str = "info") -> str:
    cls = {"pass": "pass", "fail": "fail", "warn": "warn", "info": "info"}.get(kind, "info")
    return f'<span class="badge {cls}">{_e(text)}</span>'


def _table(headers: list[str], rows: list[list], empty_msg: str = "None found.") -> str:
    if not rows:
        return f'<p class="empty">{_e(empty_msg)}</p>'
    th = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(c)}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def _section(title: str, body: str) -> str:
    return f'<section class="card"><h2>{_e(title)}</h2>{body}</section>'


_CSS = """
*{box-sizing:border-box}
body{font-family:Arial,Helvetica,sans-serif;max-width:960px;margin:0 auto;
  padding:24px 16px;color:#1a1a1a;background:#f5f6fa}
h1{font-size:1.7em;margin-bottom:4px}
h2{font-size:1.1em;color:#0044cc;margin:0 0 12px}
.meta{color:#555;font-size:.9em;margin-bottom:20px}
.summary-grid{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px}
.stat-box{background:#fff;border:1px solid #dde;border-radius:8px;
  padding:12px 20px;text-align:center;min-width:90px}
.stat-num{font-size:2em;font-weight:bold;color:#0044cc}
.stat-lbl{font-size:.75em;color:#666;margin-top:2px}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;
  font-weight:600;font-size:.85em}
.badge.pass{background:#d4edda;color:#155724}
.badge.fail{background:#f8d7da;color:#721c24}
.badge.warn{background:#fff3cd;color:#856404}
.badge.info{background:#d1ecf1;color:#0c5460}
.card{background:#fff;border:1px solid #dde;border-radius:8px;
  padding:16px 20px;margin-bottom:16px}
table{border-collapse:collapse;width:100%;font-size:.9em}
th{background:#eef2ff;font-weight:600;text-align:left;padding:7px 10px;
  border:1px solid #ccd}
td{padding:6px 10px;border:1px solid #dde;vertical-align:top}
tr:nth-child(even) td{background:#fafbff}
.empty{color:#888;font-style:italic;font-size:.9em}
.verdict{font-size:1.3em;font-weight:bold;margin-bottom:8px}
footer{color:#aaa;font-size:.78em;margin-top:32px;border-top:1px solid #dde;
  padding-top:12px}
"""


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _conformance_summary(conformance: dict) -> str:
    compliant = conformance.get("compliant", False)
    failed = conformance.get("failedRules", 0)
    verdict_badge = _badge("PASS — PDF/UA-1 Conformant", "pass") if compliant \
        else _badge(f"FAIL — {failed} veraPDF rule(s) not met", "fail")

    report = conformance.get("report", {})
    src = conformance.get("source", {})
    stats = [
        (report.get("elements", 0), "Struct elements"),
        (conformance.get("contrastFixes", 0), "Contrast fixes"),
        (conformance.get("contrastRepairs", 0), "Contrast auto-repairs"),
        (conformance.get("linkQualityFixedCount", 0), "Link fixes"),
        (conformance.get("formFixed", 0), "Form fields fixed"),
        (conformance.get("footnotePairsWired", 0), "Footnote pairs"),
        (conformance.get("verapdfRepairs", 0), "veraPDF auto-repairs"),
        (conformance.get("annotContentsFixed", 0), "Annotation fixes"),
        (conformance.get("formulasTagged", 0), "Formulas tagged"),
        (conformance.get("captionsLinked", 0), "Captions linked"),
        (conformance.get("aiAltGenerated", 0), "AI alt texts"),
        (conformance.get("fontsEmbedded", 0), "Fonts embedded"),
    ]
    grid = "".join(
        f'<div class="stat-box"><div class="stat-num">{v}</div>'
        f'<div class="stat-lbl">{_e(lbl)}</div></div>'
        for v, lbl in stats
    )
    return (
        f'<div class="verdict">{verdict_badge}</div>'
        f'<div class="summary-grid">{grid}</div>'
    )


def _verapdf_section(failures: list) -> str:
    if not failures:
        return _section("veraPDF Conformance Failures", '<p class="empty">No failures — document meets PDF/UA-1.</p>')
    rows = []
    for f in failures[:60]:
        clause = f.get("clause") or f.get("ruleId") or ""
        desc = f.get("description") or f.get("message") or ""
        loc = f.get("location") or ""
        rows.append([clause, str(desc)[:120], str(loc)[:60]])
    return _section(
        f"veraPDF Conformance Failures ({len(failures)})",
        _table(["Clause", "Description", "Location"], rows),
    )


def _contrast_section(failures: list) -> str:
    if not failures:
        return ""
    rows = [
        [
            str(f.get("page", "")),
            str(f.get("text", ""))[:40],
            f"{f.get('ratio', 0):.2f}:1",
            f"{f.get('required', 4.5):.1f}:1",
            str(f.get("font_size", "")),
        ]
        for f in failures[:30]
    ]
    return _section(
        f"Contrast Issues ({len(failures)})",
        _table(["Page", "Text (excerpt)", "Actual", "Required", "Font size"], rows),
    )


def _font_section(issues: list) -> str:
    if not issues:
        return ""
    rows = [
        [
            f.get("font_name", ""),
            f.get("issue", ""),
            _badge("error", "fail") if f.get("severity") == "error" else _badge("warning", "warn"),
        ]
        for f in issues[:20]
    ]
    return _section(
        f"Font Issues ({len(issues)})",
        _table(["Font name", "Issue", "Severity"], rows),
    )


def _alt_section(issues: list) -> str:
    if not issues:
        return ""
    rows = [
        [str(i.get("page", "")), i.get("issue", ""), str(i.get("text", ""))[:60]]
        for i in issues[:20]
    ]
    return _section(
        f"Alt Text Issues ({len(issues)})",
        _table(["Page", "Issue", "Context"], rows),
    )


def _heading_section(issues: list) -> str:
    if not issues:
        return ""
    rows = [
        [str(i.get("page", "")), i.get("issue", ""), str(i.get("text", ""))[:60]]
        for i in issues[:20]
    ]
    return _section(
        f"Heading Structure Issues ({len(issues)})",
        _table(["Page", "Issue", "Text"], rows),
    )


def _abbrev_section(abbrevs: list) -> str:
    if not abbrevs:
        return ""
    rows = [
        [a.get("term", ""), str(a.get("context", ""))[:80], str(a.get("count", ""))]
        for a in abbrevs[:20]
    ]
    return _section(
        f"Unexpanded Abbreviations ({len(abbrevs)}) — WCAG 3.1.4",
        _table(["Abbreviation", "Context", "Occurrences"], rows),
    )


def _reading_level_section(rl: dict) -> str:
    if not rl:
        return ""
    grade = rl.get("grade_level", "N/A")
    ease = rl.get("flesch_ease", "N/A")
    sev = rl.get("severity", "ok")
    kind = {"warning": "warn", "error": "fail"}.get(sev, "info")
    return _section(
        "Reading Level — WCAG 3.1.5",
        f"<p>Flesch-Kincaid Grade Level: <strong>{_e(grade)}</strong>&emsp;"
        f"Flesch Reading Ease: <strong>{_e(ease)}</strong>&emsp;"
        f"{_badge(sev.upper(), kind)}</p>"
        f"<p>{_e(rl.get('description', ''))}</p>",
    )


def _security_section(security: dict) -> str:
    if not security or not security.get("encrypted"):
        return ""
    sev = security.get("severity", "ok")
    kind = {"error": "fail", "warning": "warn"}.get(sev, "info")
    return _section(
        "Encryption / Security — PDF/UA §7.16",
        f"<p>{_badge(sev.upper(), kind)} {_e(security.get('description', ''))}</p>",
    )


def _ocg_section(issues: list) -> str:
    if not issues:
        return ""
    rows = [
        [i.get("type", ""), i.get("layer", "—"), i.get("description", "")]
        for i in issues
    ]
    return _section(
        f"Optional Content / Layers ({len(issues)}) — PDF/UA §7.11",
        _table(["Type", "Layer", "Description"], rows),
    )


def _link_section(issues: list) -> str:
    if not issues:
        return ""
    rows = [
        [str(i.get("page", "")), i.get("issue", ""), str(i.get("url", ""))[:60]]
        for i in issues[:20]
    ]
    return _section(
        f"Link Quality Issues ({len(issues)}) — WCAG 2.4.4",
        _table(["Page", "Issue", "URL"], rows),
    )


def _sensory_section(issues: list) -> str:
    if not issues:
        return ""
    rows = [
        [str(i.get("page", "")), i.get("issue", ""), str(i.get("text", ""))[:80]]
        for i in issues[:20]
    ]
    return _section(
        f"Sensory Characteristic Issues ({len(issues)}) — WCAG 1.3.3",
        _table(["Page", "Issue", "Text"], rows),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def generate_report(filename: str, conformance: dict) -> str:
    """Return a self-contained HTML accessibility audit report string."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sections = "\n".join([
        _verapdf_section(conformance.get("failures", [])),
        _contrast_section(conformance.get("contrastFailures", [])),
        _font_section(conformance.get("fontIssues", [])),
        _alt_section(conformance.get("altIssues", [])),
        _heading_section(conformance.get("headingIssues", [])),
        _link_section(conformance.get("linkQualityIssues", [])),
        _sensory_section(conformance.get("sensoryIssues", [])),
        _abbrev_section(conformance.get("abbreviations", [])),
        _reading_level_section(conformance.get("readingLevel", {})),
        _security_section(conformance.get("security", {})),
        _ocg_section(conformance.get("ocgIssues", [])),
    ])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Accessibility Audit — {_e(filename)}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>PDF Accessibility Audit Report</h1>
<p class="meta"><strong>File:</strong> {_e(filename)}&emsp;
<strong>Generated:</strong> {_e(now)}</p>
{_conformance_summary(conformance)}
{sections}
<footer>
Generated by PDF Accessibility Remediation Engine &middot; WCAG 2.2 AA &middot; PDF/UA-1
</footer>
</body>
</html>"""
