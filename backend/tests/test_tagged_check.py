"""tagged_check.py — repair-vs-rebuild routing.

Regression guard for the counting bug: structure elements are identified by
their /S key, not by the optional /Type /StructElem, so a tree whose elements
omit /Type must still count as tagged.
"""

from __future__ import annotations

import pytest

pikepdf = pytest.importorskip("pikepdf")

from app.tagged_check import assess_tagging, _count_struct_elements, MIN_STRUCT_ELEMENTS


def test_untagged_pdf_routes_to_rebuild(tmp_path):
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    path = str(tmp_path / "u.pdf")
    pdf.save(path)
    info = assess_tagging(path)
    assert info["tagged"] is False
    assert info["elements"] == 0


def test_elements_counted_by_S_key_without_Type(make_table_pdf):
    # make_table_pdf sets /Type on elements; strip them to prove counting by /S.
    path = make_table_pdf([["TH", "TH", "TH"]] + [["TH", "TD", "TD"]] * 6)
    pdf = pikepdf.open(path, allow_overwriting_input=True)
    from pikepdf import Name
    seen = set(); stack = [pdf.Root.StructTreeRoot]
    while stack:
        o = stack.pop()
        if not hasattr(o, "get"):
            continue
        og = getattr(o, "objgen", None)
        if og and og in seen:
            continue
        if og:
            seen.add(og)
        if "/Type" in o:
            del o[Name("/Type")]
        k = o.get("/K")
        if k is not None:
            stack.extend(list(k) if isinstance(k, pikepdf.Array) else [k])
    pdf.save()
    pdf.close()

    info = assess_tagging(path)
    assert info["tagged"] is True
    assert info["elements"] >= MIN_STRUCT_ELEMENTS


def test_stub_tree_is_not_tagged(tmp_path):
    from pikepdf import Array, Dictionary, Name
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=root))
    root.K = Array([doc])
    pdf.Root.StructTreeRoot = root
    path = str(tmp_path / "stub.pdf")
    pdf.save(path); pdf.close()
    info = assess_tagging(path)
    # one lone Document element is below the meaningful-structure threshold
    assert info["tagged"] is False
