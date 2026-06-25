"""Formula / mathematical expression detection — Sprint 21 (PDF/UA 7.8).

Scans text nodes for mathematical content and re-tags them as Formula
struct elements with an /Alt attribute describing the expression.

PDF/UA-1 §7.8: all mathematical expressions shall be tagged with the
Formula structure type and provide a text alternative via /Alt.

Heuristics used:
  - LaTeX command sequences (\\frac, \\sum, \\int, ...)
  - Unicode math symbols (∑ ∫ √ ∞ ± × ÷ ≤ ≥ ≠ ≈ ∈ ∉ ...)
  - Equation numbering patterns: (1), (2.3), [A.1]
  - Inline variable-with-subscript/superscript patterns
"""

from __future__ import annotations

import re

_LATEX = re.compile(
    r"\\(?:frac|sum|int|sqrt|lim|prod|alpha|beta|gamma|delta|epsilon|"
    r"theta|lambda|mu|pi|sigma|omega|infty|partial|nabla|cdot|times|"
    r"div|pm|leq|geq|neq|approx|in|notin|subset|cup|cap|forall|exists)"
    r"|\\\{|\\\}|\\left|\\right",
    re.IGNORECASE,
)
_MATH_SYMS = re.compile(
    r"[∑∫√∞±×÷≤≥≠≈∈∉∩∪∀∃→←↔⊕⊗⊆⊇∂∇·°′″ℝℂℤℕℚ]"
)
_EQ_NUM = re.compile(r"^\s*[\(\[]\s*\d+[\.\d]*\s*[\)\]]\s*$")
_INLINE_MATH = re.compile(
    r"\b[a-zA-Z]\s*[=\+\-\*\/\^]\s*[a-zA-Z0-9]|"
    r"[a-zA-Z]_\{?\d+\}?|"
    r"[a-zA-Z]\^\{?[\d\-\+]\}?"
)
_MAX_LEN = 400   # skip long prose paragraphs
_MIN_MATH_SYMS = 2


def _is_formula(text: str) -> bool:
    if not text or len(text) > _MAX_LEN:
        return False
    if _LATEX.search(text):
        return True
    syms = _MATH_SYMS.findall(text)
    if len(syms) >= _MIN_MATH_SYMS:
        return True
    if _EQ_NUM.match(text):
        return True
    # Only apply inline heuristic if text is short (likely an expression, not prose)
    if len(text) <= 80 and _INLINE_MATH.search(text):
        # Additional guard: must not look like a normal sentence
        words = [w for w in text.split() if w.isalpha() and len(w) > 2]
        if len(words) < 3:
            return True
    return False


def _alt_text(text: str) -> str:
    clean = text.strip()
    if len(clean) <= 80:
        return f"Mathematical expression: {clean}"
    return f"Mathematical expression: {clean[:77]}…"


def _tag_node(node: dict) -> bool:
    """Tag a single node if it looks like a formula. Returns True if tagged."""
    if node.get("tag") == "Formula":
        return False
    if node.get("tag") not in ("P", "Span", "TD", "TH", "LBody", "Caption"):
        return False
    text = (node.get("text") or "").strip()
    if _is_formula(text):
        node["tag"] = "Formula"
        if not node.get("alt"):
            node["alt"] = _alt_text(text)
        return True
    return False


def tag_formulas(manifest: dict) -> tuple[dict, int]:
    """Re-tag formula-like text nodes as Formula struct elements.

    Returns (updated_manifest, formulas_tagged_count).
    """
    tagged = 0

    def _walk(nodes: list) -> None:
        nonlocal tagged
        for node in nodes or []:
            if _tag_node(node):
                tagged += 1
            _walk(node.get("children", []))

    _walk(manifest.get("nodes", []))
    manifest.setdefault("source", {})["formulasTagged"] = tagged
    return manifest, tagged
