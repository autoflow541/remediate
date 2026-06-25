"""Tests for formula_tag.py -- Sprint 21."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.formula_tag import tag_formulas, _is_formula


class TestIsFormula:
    def test_latex_command(self):
        assert _is_formula(r"\frac{a}{b}")
        assert _is_formula(r"\sum_{i=1}^{n} x_i")
        assert _is_formula(r"\int_0^1 f(x) dx")

    def test_unicode_math_symbols(self):
        # Unicode math chars (not ASCII alternatives)
        assert _is_formula("x ≤ y ≥ z ≠ 0")
        assert _is_formula("∑ a_i + ∫ f(x) dx")

    def test_equation_number_only(self):
        assert _is_formula("(1)")
        assert _is_formula("(2.3)")
        assert _is_formula("[1.4]")

    def test_plain_text_not_formula(self):
        assert not _is_formula("This is a normal sentence.")
        assert not _is_formula("Introduction")
        assert not _is_formula("")


class TestTagFormulas:
    def test_tags_latex_paragraph(self):
        manifest = {"nodes": [{"tag": "P", "text": r"\frac{a}{b} = c", "page": 1}]}
        out, count = tag_formulas(manifest)
        assert count == 1
        assert out["nodes"][0]["tag"] == "Formula"

    def test_tags_unicode_math(self):
        manifest = {"nodes": [{"tag": "P", "text": "∑ a_i = ∫ f(x) dx ≤ 1", "page": 1}]}
        out, count = tag_formulas(manifest)
        assert count == 1
        assert out["nodes"][0]["tag"] == "Formula"

    def test_leaves_plain_text_alone(self):
        manifest = {"nodes": [{"tag": "P", "text": "Normal paragraph.", "page": 1}]}
        out, count = tag_formulas(manifest)
        assert count == 0
        assert out["nodes"][0]["tag"] == "P"

    def test_recurses_into_children(self):
        manifest = {"nodes": [{"tag": "LI", "text": "", "page": 1, "children": [
            {"tag": "LBody", "text": r"\sqrt{x} + \int y dy", "page": 1}]}]}
        out, count = tag_formulas(manifest)
        assert count == 1
        assert out["nodes"][0]["children"][0]["tag"] == "Formula"

    def test_alt_text_added(self):
        manifest = {"nodes": [{"tag": "P", "text": r"\sigma = \sqrt{\frac{1}{n}}", "page": 1}]}
        out, _ = tag_formulas(manifest)
        assert out["nodes"][0].get("alt", "").startswith("Mathematical expression")

    def test_empty_manifest(self):
        out, count = tag_formulas({"nodes": []})
        assert count == 0
