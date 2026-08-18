"""End-to-end: heading_check and reading_order now recover element text from
the content stream (via mc_text), instead of leaving it blank."""

from __future__ import annotations

import pytest

pikepdf = pytest.importorskip("pikepdf")
from pikepdf import Array, Dictionary, Name  # noqa: E402

from app.heading_check import check_headings  # noqa: E402
from app.reading_order import extract_reading_order  # noqa: E402


def _save(pdf, tmp_path, name="doc.pdf"):
    p = str(tmp_path / name)
    pdf.save(p)
    pdf.close()
    return p


def _new_pdf_with_content(content: bytes):
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.pages[0].obj.Contents = pdf.make_stream(content)
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    return pdf


def test_skipped_heading_issue_carries_recovered_text(tmp_path):
    pdf = _new_pdf_with_content(
        b"BT /F1 12 Tf 50 750 Td "
        b"/H1 <</MCID 0>> BDC (Introduction) Tj EMC "
        b"/H3 <</MCID 1>> BDC (Fine Details) Tj EMC ET"
    )
    page = pdf.pages[0].obj
    root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=root))
    h1 = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/H1"), P=doc, Pg=page, K=0))
    h3 = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/H3"), P=doc, Pg=page, K=1))
    doc.K = Array([h1, h3])
    root.K = Array([doc])
    pdf.Root.StructTreeRoot = root

    issues = check_headings(_save(pdf, tmp_path))
    skips = [i for i in issues if i["type"] == "skipped_level"]
    assert len(skips) == 1
    assert skips[0]["text"] == "Fine Details"     # was "" before mc_text wiring


def test_empty_heading_is_flagged(tmp_path):
    pdf = _new_pdf_with_content(
        b"BT /F1 12 Tf 50 750 Td /H1 <</MCID 0>> BDC (Title) Tj EMC ET"
    )
    page = pdf.pages[0].obj
    root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=root))
    h1 = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/H1"), P=doc, Pg=page, K=0))
    # H2 with no /Alt, no /K, no children -> genuinely empty.
    h2 = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/H2"), P=doc, Pg=page))
    doc.K = Array([h1, h2])
    root.K = Array([doc])
    pdf.Root.StructTreeRoot = root

    issues = check_headings(_save(pdf, tmp_path))
    empties = [i for i in issues if i["type"] == "empty_heading"]
    assert len(empties) == 1
    assert empties[0]["level"] == 2


def test_non_empty_heading_not_flagged_empty(tmp_path):
    pdf = _new_pdf_with_content(
        b"BT /F1 12 Tf 50 750 Td /H1 <</MCID 0>> BDC (Real Title) Tj EMC ET"
    )
    page = pdf.pages[0].obj
    root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=root))
    h1 = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/H1"), P=doc, Pg=page, K=0))
    doc.K = Array([h1])
    root.K = Array([doc])
    pdf.Root.StructTreeRoot = root

    issues = check_headings(_save(pdf, tmp_path))
    assert not any(i["type"] == "empty_heading" for i in issues)


def test_reading_order_preview_recovered_from_content(tmp_path):
    pdf = _new_pdf_with_content(
        b"BT /F1 12 Tf 50 750 Td /P <</MCID 0>> BDC (A body paragraph.) Tj EMC ET"
    )
    page = pdf.pages[0].obj
    root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=root))
    p = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/P"), P=doc, Pg=page, K=0))
    doc.K = Array([p])
    root.K = Array([doc])
    pdf.Root.StructTreeRoot = root

    elements = extract_reading_order(_save(pdf, tmp_path))
    assert len(elements) == 1
    assert elements[0]["preview"] == "A body paragraph."   # was "" before
