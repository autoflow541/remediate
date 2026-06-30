"""AI composite accessibility score — Sprint 11.

Uses Claude (Haiku) to evaluate the autotag manifest holistically and produce:
  - A letter grade  (A+ / A / B / C / D / F)
  - A numeric score (0–100)
  - A short narrative summary (2–4 sentences)
  - Up to 5 prioritised action items

The score is computed from a weighted combination of machine-checkable signals
PLUS Claude's qualitative assessment of alt text quality, heading logic, and
overall structural completeness.

Falls back gracefully (returns None) when ANTHROPIC_API_KEY is not set or
the anthropic package is not installed.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

# ── Weighted signal extraction ────────────────────────────────────────────────

def _extract_signals(manifest: dict) -> dict[str, Any]:
    """Pull numeric signals from the manifest for the scoring prompt."""
    src = manifest.get("source", {})
    nodes = manifest.get("nodes", [])

    total_nodes = len(nodes)
    figures = [n for n in nodes if n.get("tag") in ("Figure", "Image")]
    figures_with_alt = [f for f in figures if (f.get("alt") or "").strip() not in ("", "image", "figure")]
    headings = [n for n in nodes if n.get("tag", "").startswith("H")]
    links = [n for n in nodes if n.get("tag") == "Link"]

    return {
        # Structure
        "totalNodes": total_nodes,
        "headingCount": len(headings),
        "paragraphCount": len([n for n in nodes if n.get("tag") == "P"]),
        "tableCount": len([n for n in nodes if n.get("tag") == "Table"]),
        "listCount": len([n for n in nodes if n.get("tag") in ("L", "LI")]),
        # Images
        "figureCount": len(figures),
        "figuresWithAlt": len(figures_with_alt),
        "altCoveragePercent": (
            round(100 * len(figures_with_alt) / len(figures)) if figures else 100
        ),
        # Issues from autotag
        "altIssueCount": manifest.get("altIssueCount", 0),
        "headingIssueCount": manifest.get("headingIssueCount", 0),
        "colorOnlyCount": manifest.get("colorOnlyCount", 0),
        "sensoryIssueCount": manifest.get("sensoryIssueCount", 0),
        "fontIssueCount": manifest.get("fontIssueCount", 0),
        "contrastIssueCount": manifest.get("contrastIssueCount", 0),
        "linkTextIssueCount": manifest.get("linkTextIssueCount", 0),
        "tableStructureIssueCount": manifest.get("tableStructureIssueCount", 0),
        "languageIssueCount": manifest.get("languageIssueCount", 0),
        "metadataIssueCount": manifest.get("metadataIssueCount", 0),
        # Metadata
        "hasTitle": bool(src.get("title", "").strip()),
        "hasLang": bool(src.get("lang", "").strip()),
        "isTagged": src.get("tagged", False),
        "pageCount": src.get("pageCount", 1),
        # Engine
        "engine": src.get("engine", "unknown"),
        "conformanceScore": manifest.get("conformanceScore"),
    }


_SYSTEM = """\
You are an expert PDF accessibility auditor. You evaluate documents against
WCAG 2.2 AA and PDF/UA-1 (ISO 14289-1) standards.

Given a JSON object of signals extracted from a PDF accessibility manifest,
produce a JSON response with exactly this structure:

{
  "grade": "<A+|A|B|C|D|F>",
  "score": <integer 0-100>,
  "summary": "<2-4 sentence narrative suitable for a non-technical document owner>",
  "priorities": [
    "<concise action item 1>",
    "<concise action item 2>",
    ...up to 5 items
  ]
}

Grading rubric:
  A+ (95-100): Virtually no issues; production-ready accessible document
  A  (85-94):  Minor issues only; excellent baseline
  B  (70-84):  Several moderate issues; usable but needs attention
  C  (50-69):  Multiple significant issues; remediation required before publishing
  D  (30-49):  Serious accessibility failures; major rework needed
  F  (0-29):   Document is not accessible; foundational issues throughout

Weight the most impactful issues heavily:
  - Missing alt text on figures       (high weight)
  - No document language              (high weight)
  - No structure tags at all          (critical — cap at D)
  - Missing heading structure         (medium weight)
  - Font embedding failures           (medium weight)
  - Contrast failures                 (medium weight)
  - Link text issues                  (low-medium weight)
  - Table structure issues            (low-medium weight)
  - Sensory / color-only warnings     (low weight — advisory only)

If isTagged is false, the document has no structure at all — cap score at 30.
If altCoveragePercent < 50 and figureCount > 2, reduce score significantly.

Return ONLY the JSON object, no other text.
"""


def score_accessibility(manifest: dict) -> dict[str, Any] | None:
    """Return an AI accessibility score dict, or None on failure.

    Result keys: grade, score, summary, priorities.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    log.warning("ai_score: called, key_len=%d", len(api_key))
    if not api_key:
        log.warning("ai_score: no key, returning None")
        return None

    try:
        import anthropic
    except ImportError:
        return None

    try:
        signals = _extract_signals(manifest)
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(signals, indent=2),
                }
            ],
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        result = json.loads(raw)

        # Validate structure
        for key in ("grade", "score", "summary", "priorities"):
            if key not in result:
                raise ValueError(f"Missing key: {key}")

        result["score"] = int(result["score"])
        result["priorities"] = list(result.get("priorities", []))[:5]

        return result

    except Exception as exc:
        import traceback as _tb
        log.warning("ai_score: scoring failed: %s\n%s", exc, _tb.format_exc())
        return None
