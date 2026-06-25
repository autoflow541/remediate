"""Per-section language detection for PDF manifest nodes.

WCAG 3.1.2 (Language of Parts) requires that passages in languages other than
the document's primary language carry a /Lang attribute so screen readers can
switch voices/pronunciations mid-document.

This module walks the manifest node tree, extracts text from paragraph-level
structural elements, and uses langdetect to identify the language.  Nodes
whose detected language differs from the document language receive a
``language`` key in the manifest dict, which writeback.py converts into a
PDF /Lang entry in the structure tree.

Dependencies:
  pip install langdetect   (added to requirements.txt)

Graceful degradation: if langdetect is not installed the function returns 0
and the manifest is unchanged, so the pipeline never hard-fails.
"""

from __future__ import annotations

MIN_TEXT_LEN = 40   # characters — shorter spans are too ambiguous to classify reliably

# Structural element tags that carry readable body text
_TEXT_TAGS = frozenset({
    "P", "H1", "H2", "H3", "H4", "H5", "H6",
    "Caption", "LI", "Lbl", "LBody",
})


def _iter_nodes(nodes: list[dict]):
    """Depth-first walk of the manifest node tree."""
    for node in nodes:
        yield node
        children = node.get("children") or []
        if children:
            yield from _iter_nodes(children)


def _doc_lang_base(manifest: dict) -> str:
    """Return the ISO 639-1 base code of the document language, e.g. 'en'."""
    lang = (
        manifest.get("source", {}).get("language")
        or manifest.get("language")
        or "en"
    )
    return lang.split("-")[0].lower()


def detect_node_languages(manifest: dict) -> tuple[dict, int]:
    """Walk manifest nodes and annotate those in a different language.

    Returns (updated_manifest, number_of_nodes_annotated).
    The manifest is mutated in place (nodes dicts are shared references), so
    the caller can use either the returned value or the original object.
    """
    try:
        from langdetect import detect, LangDetectException  # type: ignore
    except ImportError:
        return manifest, 0

    doc_lang = _doc_lang_base(manifest)
    annotated = 0

    for node in _iter_nodes(manifest.get("nodes", [])):
        tag = node.get("tag", "")
        if tag not in _TEXT_TAGS:
            continue

        text = (node.get("text") or "").strip()
        if len(text) < MIN_TEXT_LEN:
            continue

        try:
            detected = detect(text)
        except Exception:
            continue

        if detected and detected != doc_lang:
            node["language"] = detected
            annotated += 1

    return manifest, annotated
