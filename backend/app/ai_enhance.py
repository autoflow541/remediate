"""AI-powered content enhancement pass (Sprints 12-13).

Runs after the main autotag pipeline and uses Claude Haiku for:
  1. Table summaries — generates a /Summary attribute on complex tables
     so screen readers can announce what the table contains before reading
     each cell (PDF/UA-1 best practice; referenced by WCAG 1.3.1).
  2. Alt text quality scoring — scores existing Figure alt text 1-5 and
     suggests improvements for low-scoring descriptions (WCAG 1.1.1).

Both passes are optional (degrade silently without ANTHROPIC_API_KEY).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re

log = logging.getLogger(__name__)

MODEL      = "claude-haiku-4-5-20251001"
MAX_TOKENS = 512


# ── Shared helper ─────────────────────────────────────────────────────────────

def _call_text(client, prompt: str) -> str | None:
    """Text-only Claude call. Returns response text or None."""
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as exc:
        log.debug("ai_enhance text call failed: %s", exc)
        return None


def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return None


# ── 1. Table summaries ────────────────────────────────────────────────────────

_TABLE_SUMMARY_PROMPT = """You are writing a brief table summary for an accessible PDF.

A screen reader will read this summary before reading the table cells, so the
user knows what the table is about and how to navigate it.

Table cell content (first 3 rows):
{cells}

Write a one or two sentence summary that answers:
  • What data does this table contain?
  • How many rows/columns (if notable)?
  • What should the reader look for?

Return ONLY the summary text (no JSON, no quotes, no labels)."""


def _collect_table_cells(table: dict, max_rows: int = 3) -> str:
    """Extract first N rows of a table as a text snippet."""
    rows = []
    for tr in (table.get("children") or []):
        if tr.get("tag") != "TR":
            continue
        cells = []
        for cell in (tr.get("children") or []):
            text = (cell.get("text") or "").strip()[:40]
            if text:
                cells.append(text)
        if cells:
            rows.append(" | ".join(cells))
        if len(rows) >= max_rows:
            break
    return "\n".join(rows)


def _flat_nodes(nodes: list[dict]):
    for n in nodes:
        yield n
        yield from _flat_nodes(n.get("children") or [])


def _add_table_summaries(client, manifest: dict) -> tuple[dict, int]:
    """Generate summaries for tables with > 2 rows and no existing summary."""
    nodes = manifest.get("nodes", [])
    count = 0

    def _patch_tables(ns: list[dict]) -> list[dict]:
        nonlocal count
        result = []
        for n in ns:
            if n.get("tag") == "Table" and not n.get("summary") and not n.get("decorative"):
                rows = [c for c in (n.get("children") or []) if c.get("tag") == "TR"]
                if len(rows) >= 3:
                    cells_text = _collect_table_cells(n)
                    if cells_text:
                        prompt = _TABLE_SUMMARY_PROMPT.format(cells=cells_text)
                        summary = _call_text(client, prompt)
                        if summary and len(summary) > 10:
                            n = {**n, "summary": summary}
                            count += 1
            if n.get("children"):
                n = {**n, "children": _patch_tables(n["children"])}
            result.append(n)
        return result

    new_nodes = _patch_tables(nodes)
    return {**manifest, "nodes": new_nodes}, count


# ── 2. Alt text quality scoring ───────────────────────────────────────────────

_ALT_SCORE_PROMPT = """You are evaluating alt text quality for an accessible PDF.

Image alt text: "{alt}"

Rate this alt text on a scale of 1-5 where:
  1 = Useless (empty, generic like "image", "figure", "photo")
  2 = Too vague (describes what it IS not what it SHOWS, e.g. "a chart")
  3 = Minimal (partially descriptive but missing key info)
  4 = Good (descriptive, conveys the meaning of the image)
  5 = Excellent (fully descriptive, includes all relevant content and context)

Return JSON: {{"score": N, "suggestion": "improved alt text if score < 4, else empty string"}}
Return ONLY valid JSON."""


def _score_alt_texts(client, manifest: dict) -> tuple[dict, list[dict]]:
    """Score existing alt texts and flag low-quality ones."""
    nodes = manifest.get("nodes", [])
    scored: list[dict] = []

    def _walk(ns: list[dict]) -> list[dict]:
        result = []
        for n in ns:
            if n.get("tag") == "Figure" and n.get("alt") and not n.get("decorative"):
                alt = n["alt"]
                prompt = _ALT_SCORE_PROMPT.format(alt=alt[:200])
                raw = _call_text(client, prompt)
                res = _parse_json(raw or "")
                if res and isinstance(res.get("score"), (int, float)):
                    score = int(res["score"])
                    suggestion = (res.get("suggestion") or "").strip()
                    if score < 4:
                        scored.append({
                            "nodeId": n["id"],
                            "page": n.get("page"),
                            "current_alt": alt[:80],
                            "score": score,
                            "suggestion": suggestion,
                            "description": (
                                f"Alt text scored {score}/5. "
                                + (f'Suggested: "{suggestion[:100]}"' if suggestion else
                                   "Consider a more descriptive alt text.")
                            ),
                        })
                        if suggestion and score <= 2:
                            # Auto-upgrade very poor alt text with AI suggestion
                            n = {**n, "alt": suggestion, "altImproved": True}
            if n.get("children"):
                n = {**n, "children": _walk(n["children"])}
            result.append(n)
        return result

    new_nodes = _walk(nodes)
    return {**manifest, "nodes": new_nodes}, scored


# ── Main entry point ───────────────────────────────────────────────────────────

def enhance_manifest(manifest: dict) -> dict:
    """Run table summary + alt text scoring passes.

    Returns manifest unchanged if ANTHROPIC_API_KEY is not set.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return manifest

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        return manifest

    stats: dict[str, int] = {"table_summaries": 0, "alt_improved": 0, "alt_flagged": 0}

    try:
        manifest, table_count = _add_table_summaries(client, manifest)
        stats["table_summaries"] = table_count
    except Exception as exc:
        log.debug("Table summary pass failed: %s", exc)

    try:
        manifest, alt_issues = _score_alt_texts(client, manifest)
        stats["alt_flagged"] = len(alt_issues)
        stats["alt_improved"] = sum(1 for a in alt_issues if a.get("score", 5) <= 2)
        # Store low-quality alt flags in manifest source for audit report
        if alt_issues:
            manifest.setdefault("source", {})["altQualityIssues"] = alt_issues
    except Exception as exc:
        log.debug("Alt scoring pass failed: %s", exc)

    log.info(
        "ai_enhance: table_summaries=%d alt_flagged=%d alt_improved=%d",
        stats["table_summaries"], stats["alt_flagged"], stats["alt_improved"],
    )

    src = {**manifest.get("source", {}), "aiEnhance": stats}
    return {**manifest, "source": src}
