"""Plain-language explanations for PDF/UA-1 veraPDF failure clauses.

veraPDF reports failures using ISO 14289-1 (PDF/UA-1) clause numbers.
These codes are meaningful to PDF engineers but opaque to accessibility
coordinators and document authors.  This module maps each clause to:
  - A short human-readable title
  - A one-sentence explanation of what it means
  - A short remediation hint

The explanations are used by the Studio's conformance ledger to replace
raw clause codes with actionable plain English.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ClauseExplanation:
    title: str
    explanation: str
    hint: str


# ISO 14289-1 (PDF/UA-1) clauses that veraPDF checks.
# Keys are "clause.testNumber" strings matching veraPDF report attributes.
_EXPLANATIONS: dict[str, ClauseExplanation] = {
    # ── 6.2 Tagged PDF ──────────────────────────────────────────────────────
    "6.2-1": ClauseExplanation(
        "Document not marked as Tagged PDF",
        "The PDF's catalog doesn't declare it as a Tagged PDF, so assistive "
        "technology has no structure tree to navigate.",
        "Set the /MarkInfo /Marked flag to true during document creation.",
    ),
    "6.2-2": ClauseExplanation(
        "Real content not tagged",
        "Some visible content (text, images, or vector graphics) exists outside "
        "the structure tree and will be invisible to screen readers.",
        "Ensure every content item is referenced by a structure element or "
        "explicitly marked as an Artifact.",
    ),
    "6.2-3": ClauseExplanation(
        "Content marked as Artifact appears in structure tree",
        "An element is tagged as both a structural element and an Artifact, "
        "which is contradictory.",
        "Remove the content from the structure tree or remove the Artifact mark.",
    ),
    # ── 6.3 Document outline (bookmarks) ────────────────────────────────────
    "6.3-1": ClauseExplanation(
        "Missing or incomplete document outline",
        "The PDF has more than 20 pages but no bookmark (outline) tree, making "
        "navigation difficult for keyboard and screen-reader users.",
        "Add bookmarks for each major section heading.",
    ),
    # ── 6.4 Semantic elements ────────────────────────────────────────────────
    "6.4-1": ClauseExplanation(
        "Non-standard structure type without role mapping",
        "A custom tag is used but has no role map entry pointing to a standard "
        "PDF/UA tag, so assistive technology can't interpret its meaning.",
        "Add a /RoleMap entry that maps the custom tag to a standard PDF tag "
        "(e.g., <CustomH1> → <H1>).",
    ),
    "6.4-2": ClauseExplanation(
        "Incorrect nesting of heading levels",
        "Heading levels are skipped or out of order (e.g., <H1> followed directly "
        "by <H3>), breaking the document outline for screen reader users.",
        "Ensure headings follow a strict hierarchy: H1 → H2 → H3, with no gaps.",
    ),
    "6.4-3": ClauseExplanation(
        "Table missing header cells",
        "A data table is present but has no <TH> (table header) cells, so "
        "screen readers cannot associate data cells with their headers.",
        "Mark column and row headers as <TH> elements with /Scope attributes.",
    ),
    # ── 6.5 Headings ─────────────────────────────────────────────────────────
    "6.5-1": ClauseExplanation(
        "Appropriate nesting of headings required",
        "Heading elements do not nest correctly within the document's structure "
        "tree, making the logical outline ambiguous.",
        "Place each heading in a Section or Document container, not floating at "
        "the top level.",
    ),
    # ── 6.7 Lists ────────────────────────────────────────────────────────────
    "6.7-1": ClauseExplanation(
        "List structure is invalid",
        "A list (<L>) contains children that are not <LI> items, or an <LI> "
        "contains unexpected direct children.",
        "Wrap list content in <LI> → <Lbl> + <LBody> pairs.",
    ),
    # ── 6.8 Math ─────────────────────────────────────────────────────────────
    "6.8-1": ClauseExplanation(
        "Mathematical formula without alternative text",
        "A formula figure has no alt-text or associated MathML, so screen "
        "readers can only announce 'image'.",
        "Add an /Alt attribute describing the formula, or embed MathML.",
    ),
    # ── 7.1 General (viewable/printable) ────────────────────────────────────
    "7.1-1": ClauseExplanation(
        "Document language not specified",
        "The /Lang entry is missing from the document catalog, so screen "
        "readers cannot select the correct voice or pronunciation rules.",
        "Set the document language (e.g., 'en-US') in the PDF catalog /Lang key.",
    ),
    "7.1-2": ClauseExplanation(
        "Title missing from document metadata",
        "The PDF's metadata has no /Title entry; screen readers announce the "
        "file name instead of a meaningful title.",
        "Add a descriptive title to the document's XMP or DocInfo metadata.",
    ),
    "7.1-3": ClauseExplanation(
        "DisplayDocTitle flag not set",
        "Even if /Title is present, the viewer is not instructed to display it "
        "in the title bar instead of the file name.",
        "Set /ViewerPreferences /DisplayDocTitle to true.",
    ),
    "7.1-4": ClauseExplanation(
        "PDF/UA identifier missing",
        "The PDF's XMP metadata is missing the pdfuaid:part = '1' declaration "
        "that signals conformance to PDF/UA-1.",
        "Add <pdfuaid:part>1</pdfuaid:part> to the document's XMP metadata stream.",
    ),
    "7.1-5": ClauseExplanation(
        "Page size or rotation not declared",
        "The /Rotate entry or MediaBox dimensions are inconsistent, which can "
        "confuse AT about the page orientation.",
        "Ensure all pages have consistent MediaBox dimensions and explicit "
        "/Rotate entries where needed.",
    ),
    "7.1-6": ClauseExplanation(
        "Encryption prevents AT access",
        "The PDF is encrypted in a way that blocks text extraction, which "
        "prevents screen readers from reading the content.",
        "Use encryption settings that permit content copying / text extraction.",
    ),
    # ── 7.2 Text ─────────────────────────────────────────────────────────────
    "7.2-1": ClauseExplanation(
        "Natural language cannot be determined for text",
        "A text span has no language set and the document /Lang is also missing, "
        "so screen readers can't select the right pronunciation engine.",
        "Set the document /Lang and, for foreign-language passages, set /Lang "
        "on the individual structure element.",
    ),
    "7.2-2": ClauseExplanation(
        "Character encoding cannot be mapped to Unicode",
        "Some characters in the PDF cannot be mapped to Unicode code points, "
        "so screen readers will misread or skip them.",
        "Ensure all fonts embed a /ToUnicode CMap, or use standard encodings.",
    ),
    # ── 7.3 Graphics ─────────────────────────────────────────────────────────
    "7.3-1": ClauseExplanation(
        "Figure missing alternative text",
        "An image (<Figure>) has no /Alt attribute, so screen readers cannot "
        "describe it to blind users.",
        "Add an /Alt attribute with a concise, meaningful description to every "
        "non-decorative image.",
    ),
    "7.3-2": ClauseExplanation(
        "Decorative graphic not marked as Artifact",
        "A purely decorative image is included in the structure tree instead "
        "of being marked as an Artifact, causing unnecessary AT announcements.",
        "Mark decorative images as Artifacts so screen readers skip them.",
    ),
    # ── 7.4 Headings ─────────────────────────────────────────────────────────
    "7.4-1": ClauseExplanation(
        "Heading elements skipped or duplicated",
        "The PDF has heading tags that are not in a valid nesting sequence, "
        "making the document outline unreliable.",
        "Review all heading levels and ensure a strict H1 → H2 → H3 hierarchy.",
    ),
    # ── 7.5 Tables ───────────────────────────────────────────────────────────
    "7.5-1": ClauseExplanation(
        "Table header not associated with data cells",
        "A table has <TH> cells, but they lack /Scope or /Headers attributes, "
        "so screen readers can't announce which header belongs to which data cell.",
        "Add /Scope (Row, Column, or Both) to all <TH> cells.",
    ),
    "7.5-2": ClauseExplanation(
        "Table cell spans rows or columns without scope",
        "A cell spanning multiple rows or columns lacks /Headers or /Scope, "
        "breaking data-cell-to-header associations.",
        "Add explicit /Headers attributes to merged cells identifying their "
        "corresponding <TH> IDs.",
    ),
    # ── 7.6 Lists ────────────────────────────────────────────────────────────
    "7.6-1": ClauseExplanation(
        "List structure malformed",
        "List items (<LI>) are not properly nested inside a list container (<L>), "
        "or list items are missing required children.",
        "Restructure the list as <L> → <LI> → (<Lbl> and/or <LBody>).",
    ),
    # ── 7.18 Interactive forms ───────────────────────────────────────────────
    "7.18.1-1": ClauseExplanation(
        "Form field missing tooltip (accessible name)",
        "An interactive form field has no /TU (tooltip / accessible name), so "
        "screen readers can only announce the field's internal ID.",
        "Add a /TU tooltip attribute with a descriptive label to every form field.",
    ),
    "7.18.2-1": ClauseExplanation(
        "Form field not in structure tree",
        "An interactive form field exists in the AcroForm but is not referenced "
        "from the structure tree, hiding it from AT.",
        "Include each form field as a Widget annotation linked from a <Form> "
        "structure element.",
    ),
    # ── 7.19 Notes & references ──────────────────────────────────────────────
    "7.19-1": ClauseExplanation(
        "Footnote or endnote not properly tagged",
        "A footnote or endnote exists in the document but is not tagged as a "
        "<Note> structure element, so its relationship to the main text is lost.",
        "Tag footnotes as <Note> elements with an /ID that references the "
        "in-text citation.",
    ),
    # ── 7.21 Embedded files ──────────────────────────────────────────────────
    "7.21-1": ClauseExplanation(
        "Embedded file missing description",
        "A file attachment has no /Desc (description) entry, so users can't "
        "tell what the attachment contains before opening it.",
        "Add a /Desc attribute to the embedded file specification dictionary.",
    ),
}

# Fallback for clauses not in the lookup table
_FALLBACK = ClauseExplanation(
    title="PDF/UA conformance issue",
    explanation=(
        "This clause of the PDF/UA-1 specification (ISO 14289-1) was not met. "
        "Screen readers and other assistive technology may not work correctly."
    ),
    hint="Review the veraPDF technical description below for remediation details.",
)


def explain_clause(clause: str, test_number: int | None = None) -> ClauseExplanation:
    """Return a human-readable explanation for a veraPDF failure clause.

    Tries "clause.testNumber" first, then falls back to "clause" alone,
    then returns the generic fallback.
    """
    if test_number is not None:
        key = f"{clause}-{test_number}"
        if key in _EXPLANATIONS:
            return _EXPLANATIONS[key]
    if clause in _EXPLANATIONS:
        return _EXPLANATIONS[clause]
    return _FALLBACK


def enrich_failures(failures: list[dict]) -> list[dict]:
    """Add plain-language fields to a list of serialised RuleResult dicts.

    Each dict should have 'clause' and optionally 'test_number' keys.
    Returns a new list with extra keys: plain_title, plain_explanation, plain_hint.
    """
    enriched = []
    for f in failures:
        clause = f.get("clause", "")
        test_num = f.get("test_number")
        exp = explain_clause(clause, test_num)
        enriched.append({
            **f,
            "plain_title": exp.title,
            "plain_explanation": exp.explanation,
            "plain_hint": exp.hint,
        })
    return enriched
