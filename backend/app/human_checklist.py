"""human_checklist.py — the Matterhorn human-verification checklist.

Of the Matterhorn Protocol's failure conditions, roughly a third are
machine-checkable (veraPDF covers those). The rest are judgment calls no
automated tool can certify: whether alt text is *meaningful*, whether the
reading order is *correct*, whether headings reflect the document's real
structure. Passing veraPDF while skipping those is how "compliant" PDFs still
get organizations sued.

This module turns that gap into a concrete, per-document checklist:

    build_checklist(pdf_path, ctx) -> list[item]

Each item carries the Matterhorn checkpoint id, the WCAG criterion, a plain
question a reviewer can answer, what automation already did (so the reviewer
verifies rather than redoes), and per-document evidence (the actual figures
and their alt text, the title, heading outline, etc.).

Applicability is data-driven: a document with no tables gets no table
checkpoints. The checklist is embedded in the audit report as a sign-off
record with reviewer name/date — the artifact you keep.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _struct_stats(pdf_path: str) -> dict:
    """Counts + samples from the structure tree needed to build the checklist."""
    stats = {
        "figures": [],       # [{alt, decorativeGuess}] max 20
        "headings": [],      # ["H1: text", ...] max 30
        "tables": 0,
        "th_cells": 0,
        "formulas": 0,
        "links": 0,
        "artifact_count": 0,
        "title": "",
        "lang": "",
        "elements": 0,
    }
    try:
        import pikepdf

        with pikepdf.open(pdf_path) as pdf:
            stats["lang"] = str(pdf.Root.get("/Lang", "") or "")
            try:
                stats["title"] = str(pdf.docinfo.get("/Title", "") or "")
            except Exception:
                pass

            # Reuse the element walker from the visual-fix module (tag + text).
            try:
                from .ai_visual_fix import _collect_elements
                elements = _collect_elements(pdf)
            except Exception:
                elements = []
            stats["elements"] = len(elements)
            for e in elements:
                tag = e.get("tag", "")
                if tag == "Figure" and len(stats["figures"]) < 20:
                    alt = ""
                    try:
                        alt = str(e["obj"].get("/Alt", "") or "")
                    except Exception:
                        pass
                    stats["figures"].append({"alt": alt[:160]})
                elif tag in ("H1", "H2", "H3", "H4", "H5", "H6") and len(stats["headings"]) < 30:
                    stats["headings"].append(f"{tag}: {(e.get('text') or '')[:70]}")
                elif tag == "Table":
                    stats["tables"] += 1
                elif tag == "TH":
                    stats["th_cells"] += 1
                elif tag == "Formula":
                    stats["formulas"] += 1
                elif tag == "Link":
                    stats["links"] += 1
    except Exception as exc:
        log.debug("human_checklist stats: %s", exc)
    return stats


def build_checklist(pdf_path: str, ctx: dict | None = None) -> list[dict]:
    """Build the per-document human-verification checklist.

    ``ctx`` may carry counts already computed by the endpoint (artifact count,
    color-only warnings, visual-review remaining items) to enrich items.
    """
    ctx = ctx or {}
    s = _struct_stats(pdf_path)

    vr_remaining = ctx.get("visualRemaining") or []

    def vr_notes(check_kind: str) -> list[str]:
        return [
            f"p.{it.get('page', '?')}: {str(it.get('detail', ''))[:140]}"
            for it in vr_remaining
            if it.get("check") == check_kind
        ][:5]

    items: list[dict] = []

    def add(id_, matterhorn, wcag, question, why, evidence=None, ai_flags=None, applicable=True):
        items.append({
            "id": id_,
            "matterhorn": matterhorn,
            "wcag": wcag,
            "question": question,
            "why": why,
            "evidence": evidence or [],
            "aiFlags": ai_flags or [],
            "applicable": bool(applicable),
        })

    n_fig = len(s["figures"])
    add(
        "alt-meaningful", "13-004", "WCAG 1.1.1",
        "Does each image's alt text convey the same information the image does?",
        "Automation wrote or checked alt text for phrasing, but only a person can "
        "judge whether it says what the image actually shows and why it's there.",
        evidence=[f"Figure {i+1}: “{f['alt']}”" if f["alt"] else f"Figure {i+1}: (no alt — marked decorative or missed)"
                  for i, f in enumerate(s["figures"])],
        ai_flags=vr_notes("alt_text"),
        applicable=n_fig > 0,
    )
    add(
        "decorative-correct", "01-007", "WCAG 1.1.1",
        "Is everything marked decorative truly decorative (nothing informative was hidden)?",
        "Content tagged as an artifact is invisible to screen readers. A logo may be "
        "decorative; a stamp, signature, or chart never is.",
        ai_flags=vr_notes("decorative"),
        applicable=True,
    )
    add(
        "reading-order", "09-001", "WCAG 1.3.2",
        "Read the document top to bottom in the tagged preview: does the order make sense "
        "(including sidebars, captions, and multi-column sections)?",
        "Layout analysis ordered the content and AI reviewed it, but reading order is the "
        "single most common real-world screen-reader failure.",
        ai_flags=vr_notes("reading_order"),
        applicable=s["elements"] > 1,
    )
    add(
        "headings-semantic", "14-002", "WCAG 1.3.1 / 2.4.6",
        "Do the headings match the document's actual outline (nothing prominent missed, "
        "nothing bold-but-not-a-heading promoted)?",
        "Heading levels were derived from layout and repaired for skips; whether each one "
        "is truly a heading is a judgment call.",
        evidence=s["headings"],
        ai_flags=vr_notes("headings"),
        applicable=True,
    )
    add(
        "tables-headers", "15-003", "WCAG 1.3.1",
        "For each table: are the header cells the right ones, and does every data cell "
        "read correctly with its headers?",
        f"{s['tables']} table(s), {s['th_cells']} header cell(s) were tagged from layout "
        "(first row/column heuristics + AI). Merged and irregular tables especially need eyes.",
        ai_flags=vr_notes("tables"),
        applicable=s["tables"] > 0,
    )
    add(
        "title-descriptive", "06-003", "WCAG 2.4.2",
        "Does the document title describe the document (not a filename or template name)?",
        "The title is announced first by screen readers and used in tab bars.",
        evidence=[f"Current title: “{s['title']}”" if s["title"] else "Current title: (empty)"],
        ai_flags=vr_notes("title"),
        applicable=True,
    )
    add(
        "language-correct", "11-001", "WCAG 3.1.1",
        "Is the document language correct, and are foreign-language passages tagged?",
        "Wrong language makes screen readers mispronounce every word.",
        evidence=[f"Document language: {s['lang'] or '(not set)'}"],
        applicable=True,
    )
    add(
        "links-purpose", "28-004", "WCAG 2.4.4",
        "Does each link's text say where it goes (no bare URLs or “click here”)?",
        "AI rewrote generic link text where a destination was resolvable; verify the "
        "rewrites and anything it left.",
        applicable=s["links"] > 0,
    )
    add(
        "formulas", "10-001", "WCAG 1.1.1",
        "Does each formula's text alternative read correctly?",
        "Formula tags carry auto-generated descriptions; math notation is easy to garble.",
        applicable=s["formulas"] > 0,
    )
    add(
        "color-sensory", "—", "WCAG 1.4.1 / 1.3.3",
        "Is any meaning conveyed only by color, shape, or position still understandable "
        "without seeing the page?",
        "Automation flags candidates but cannot judge meaning.",
        ai_flags=vr_notes("other"),
        applicable=bool(ctx.get("colorOnlyCount") or ctx.get("sensoryIssueCount")),
    )

    return [i for i in items if i["applicable"]]
