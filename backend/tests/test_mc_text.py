"""mc_text — recovering a structure element's visible text from the MCIDs it
references in the page content stream."""

from __future__ import annotations

import pytest

pikepdf = pytest.importorskip("pikepdf")
from pikepdf import Array, Dictionary, Name  # noqa: E402

from app.mc_text import page_mcid_texts, element_mcids  # noqa: E402


def _page(content: bytes):
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.pages[0].obj.Contents = pdf.make_stream(content)
    return pdf, pdf.pages[0]


def test_page_mcid_texts_tj_and_tj_array():
    pdf, page = _page(
        b"BT /F1 12 Tf 50 750 Td "
        b"/P <</MCID 0>> BDC (Hello world) Tj EMC "
        b"/P <</MCID 1>> BDC [(Good)-250(bye)] TJ EMC "
        b"ET"
    )
    texts = page_mcid_texts(page)
    assert texts.get(0) == "Hello world"
    assert texts.get(1) == "Goodbye"


def test_page_mcid_texts_multiple_shows_join():
    pdf, page = _page(
        b"BT /F1 12 Tf 50 750 Td "
        b"/H1 <</MCID 0>> BDC (Quarterly ) Tj (Report) Tj EMC ET"
    )
    assert page_mcid_texts(page).get(0) == "Quarterly Report"


def test_page_mcid_texts_untagged_text_ignored():
    # Text outside any BDC has no MCID and must not appear.
    pdf, page = _page(b"BT /F1 12 Tf 50 750 Td (loose text) Tj ET")
    assert page_mcid_texts(page) == {}


def test_page_mcid_texts_bad_stream_returns_empty():
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    # No Contents at all.
    assert page_mcid_texts(pdf.pages[0]) == {}


def test_element_mcids_variants():
    pdf = pikepdf.Pdf.new()
    # bare integer
    e_int = Dictionary(Type=Name.StructElem, S=Name("/P"), K=3)
    assert element_mcids(e_int) == [3]
    # array of ints (+ a child dict + OBJR that must be ignored)
    child = Dictionary(Type=Name.StructElem, S=Name("/Span"))
    objr = Dictionary(Type=Name.OBJR)
    e_arr = Dictionary(Type=Name.StructElem, S=Name("/P"),
                       K=Array([0, child, 2, objr]))
    assert element_mcids(e_arr) == [0, 2]
    # a single child dict — no direct MCID
    e_dict = Dictionary(Type=Name.StructElem, S=Name("/Sect"), K=child)
    assert element_mcids(e_dict) == []
    # no /K
    assert element_mcids(Dictionary(Type=Name.StructElem, S=Name("/P"))) == []
