"""reading_order — recursive extract + reorder at any depth.

Elements are reordered within their parent (siblings) at any level; non-struct
/K entries stay put; nodes never change parent."""

from __future__ import annotations

import pytest

pikepdf = pytest.importorskip("pikepdf")
from pikepdf import Array, Dictionary, Name, String  # noqa: E402

from app.reading_order import extract_reading_order, apply_reading_order  # noqa: E402

_CONTENT = (
    b"BT /F1 12 Tf 50 750 Td "
    b"/H1 <</MCID 0>> BDC (Title) Tj EMC "
    b"/P <</MCID 1>> BDC (First) Tj EMC "
    b"/P <</MCID 2>> BDC (Second) Tj EMC "
    b"/P <</MCID 3>> BDC (Footer) Tj EMC "
    b"ET"
)


def _nested_pdf(tmp_path, name="doc.pdf"):
    """Document -> [H1, Sect -> [P First, P Second, Figure], P Footer]."""
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.pages[0].obj.Contents = pdf.make_stream(_CONTENT)
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    page = pdf.pages[0].obj

    root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=root))
    h1 = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/H1"), P=doc, Pg=page, K=0))
    sect = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/Sect"), P=doc))
    p1 = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/P"), P=sect, Pg=page, K=1))
    p2 = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/P"), P=sect, Pg=page, K=2))
    fig = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/Figure"), P=sect,
                                       Pg=page, Alt=String("a chart")))
    pfoot = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/P"), P=doc, Pg=page, K=3))
    sect.K = Array([p1, p2, fig])
    doc.K = Array([h1, sect, pfoot])
    root.K = Array([doc])
    pdf.Root.StructTreeRoot = root

    path = str(tmp_path / name)
    pdf.save(path)
    pdf.close()
    return path


def _by_id(elements):
    return {e["id"]: e for e in elements}


def test_extract_returns_full_nested_tree(tmp_path):
    els = extract_reading_order(_nested_pdf(tmp_path))
    ids = [e["id"] for e in els]
    assert ids == ["e0", "e1", "e1_0", "e1_1", "e1_2", "e2"]

    m = _by_id(els)
    assert m["e0"]["level"] == 0 and m["e0"]["parent_id"] is None
    assert m["e0"]["preview"] == "Title"
    assert m["e1"]["type"] == "Section" and m["e1"]["children"] == 3
    assert m["e1_0"]["level"] == 1 and m["e1_0"]["parent_id"] == "e1"
    assert m["e1_0"]["preview"] == "First"
    assert m["e1_1"]["preview"] == "Second"
    assert m["e1_2"]["preview"] == "a chart"     # figure /Alt
    assert m["e2"]["preview"] == "Footer" and m["e2"]["parent_id"] is None


def test_max_depth_zero_is_top_level_only(tmp_path):
    els = extract_reading_order(_nested_pdf(tmp_path), max_depth=0)
    assert [e["id"] for e in els] == ["e0", "e1", "e2"]


def test_reorder_nested_siblings(tmp_path):
    path = _nested_pdf(tmp_path)
    # Swap the two paragraphs inside the Section.
    res = apply_reading_order(path, ["e1_1", "e1_0"])
    assert res["ok"] and res["changes_made"] == 2

    m = _by_id(extract_reading_order(path))
    # Section's children are now Second, First, chart — figure stays last.
    assert m["e1_0"]["preview"] == "Second"
    assert m["e1_1"]["preview"] == "First"
    assert m["e1_2"]["preview"] == "a chart"
    # Top level untouched.
    assert m["e0"]["preview"] == "Title"
    assert m["e2"]["preview"] == "Footer"


def test_reorder_top_level(tmp_path):
    path = _nested_pdf(tmp_path)
    res = apply_reading_order(path, ["e2", "e1", "e0"])
    assert res["ok"] and res["changes_made"] == 2

    els = extract_reading_order(path)
    # Footer paragraph is now first; its former nested section rides along.
    assert els[0]["preview"] == "Footer"
    assert els[-1]["preview"] == "Title"


def test_reorder_is_stable_when_order_matches(tmp_path):
    path = _nested_pdf(tmp_path)
    res = apply_reading_order(path, ["e0", "e1", "e2"])
    assert res["ok"] and res["changes_made"] == 0


def test_non_struct_kids_kept_in_place(tmp_path):
    # Document /K = [H1, <MCID 7>, P] — the bare integer must not move.
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    page = pdf.pages[0].obj
    root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=root))
    h1 = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/H1"), P=doc, Pg=page))
    p = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/P"), P=doc, Pg=page))
    doc.K = Array([h1, 7, p])          # e0 at pos0, int at pos1, e2 at pos2
    root.K = Array([doc])
    pdf.Root.StructTreeRoot = root
    path = str(tmp_path / "ns.pdf")
    pdf.save(path)
    pdf.close()

    res = apply_reading_order(path, ["e2", "e0"])
    assert res["ok"] and res["changes_made"] == 2

    with pikepdf.open(path) as out:
        k = out.Root.StructTreeRoot.K[0].K
        assert int(k[1]) == 7                       # integer stayed at index 1
        assert str(k[0].get("/S")) == "/P"          # P moved to front
        assert str(k[2].get("/S")) == "/H1"         # H1 moved to back
