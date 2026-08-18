"""Regression: audit-report issues must report the correct page number.

heading_check and alt_quality mapped pages by id(page.obj) and looked them up
by id(struct_elem["/Pg"]). pikepdf returns a fresh wrapper per access, so those
ids never matched and every issue came back page=None. Both now key on .objgen."""

from __future__ import annotations

import pytest

pikepdf = pytest.importorskip("pikepdf")
from pikepdf import Array, Dictionary, Name, String  # noqa: E402

from app.alt_quality import check_alt_quality  # noqa: E402
from app.heading_check import check_headings  # noqa: E402


def _save(pdf, tmp_path, name="doc.pdf"):
    p = str(tmp_path / name)
    pdf.save(p)
    pdf.close()
    return p


def test_alt_quality_reports_figure_page(tmp_path):
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))   # page 1
    pdf.add_blank_page(page_size=(612, 792))   # page 2
    page2 = pdf.pages[1].obj

    root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=root))
    fig = pdf.make_indirect(Dictionary(
        Type=Name.StructElem, S=Name.Figure, P=doc,
        Alt=String("image"),          # generic -> flagged
        Pg=page2))
    doc.K = Array([fig])
    root.K = Array([doc])
    pdf.Root.StructTreeRoot = root

    issues = check_alt_quality(_save(pdf, tmp_path))
    assert len(issues) == 1
    assert issues[0]["type"] == "generic"
    assert issues[0]["page"] == 2          # was None before the fix


def test_heading_check_reports_skip_page(tmp_path):
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))   # page 1
    pdf.add_blank_page(page_size=(612, 792))   # page 2
    page1, page2 = pdf.pages[0].obj, pdf.pages[1].obj

    root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=root))
    h1 = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/H1"), P=doc, Pg=page1))
    h3 = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/H3"), P=doc, Pg=page2))
    doc.K = Array([h1, h3])
    root.K = Array([doc])
    pdf.Root.StructTreeRoot = root

    issues = check_headings(_save(pdf, tmp_path))
    skips = [i for i in issues if i["type"] == "skipped_level"]
    assert len(skips) == 1
    assert skips[0]["level"] == 3
    assert skips[0]["page"] == 2          # was None before the fix
