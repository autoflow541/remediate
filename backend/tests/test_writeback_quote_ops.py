"""remark_page must honour the implicit line move of the ' and " text
operators, so their glyphs bind to the correct line's structure element."""

from __future__ import annotations

import pytest

pikepdf = pytest.importorskip("pikepdf")
from pikepdf import Dictionary, Name  # noqa: E402

from app.writeback import StructTreeBuilder, Leaf  # noqa: E402


def _make_leaf(pdf, bbox):
    elem = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/P")))
    return Leaf(node={"tag": "P"}, elem=elem, page=1, bbox=bbox, decorative=False)


def test_quote_operator_binds_to_next_line():
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    # Line one shown with Tj at y=750; line two shown with ' (implicit T* down
    # by the 12-unit leading) at y=738.
    content = b"BT /F1 12 Tf 12 TL 50 750 Td (Line one) Tj (Line two) ' ET"
    pdf.pages[0].obj.Contents = pdf.make_stream(content)

    wb = StructTreeBuilder(pdf, manifest={"nodes": []})
    wb._parent_tree_nums = []      # normally initialised in apply()
    line1 = _make_leaf(pdf, [40.0, 745.0, 200.0, 758.0])   # contains y=750
    line2 = _make_leaf(pdf, [40.0, 730.0, 200.0, 743.0])   # contains y=738
    wb.state.leaves = [line1, line2]

    wb.remark_page(0, pdf.pages[0])

    # Both lines bound to their own element — line two's ' text did NOT fall
    # back onto line one (which is what happened before the T* fix).
    assert line1.mcid is not None
    assert line2.mcid is not None
    assert line1.mcid != line2.mcid
