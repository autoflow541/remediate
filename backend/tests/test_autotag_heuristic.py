"""_heuristic_autotag — the PyMuPDF fallback tagger used whenever OpenDataLoader
is unavailable or returns an empty structure. It must actually produce nodes
(it previously read span['text'] from get_text('rawdict'), where that key does
not exist, and silently produced zero nodes on every document)."""

from __future__ import annotations

import pytest

pytest.importorskip("pikepdf")
pytest.importorskip("pymupdf")
import pikepdf  # noqa: E402
from pikepdf import Dictionary, Name  # noqa: E402

from app.autotag import _heuristic_autotag  # noqa: E402


def _text_pdf(tmp_path, content: bytes, name="h.pdf"):
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pg = pdf.pages[0]
    pg.obj.Contents = pdf.make_stream(content)
    pg.obj.Resources = Dictionary(Font=Dictionary(
        F1=Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica)))
    p = str(tmp_path / name)
    pdf.save(p)
    pdf.close()
    return p


def test_heuristic_produces_nodes_from_body_text(tmp_path):
    p = _text_pdf(tmp_path, b"BT /F1 12 Tf 50 700 Td (A plain body paragraph of text.) Tj ET")
    nodes = _heuristic_autotag(p)["nodes"]
    assert len(nodes) >= 1
    assert any("body paragraph" in n["text"] for n in nodes)
    assert all(n.get("page") == 1 for n in nodes)


def test_heuristic_classifies_large_text_as_heading(tmp_path):
    # A 24pt line over 12pt body -> ratio 2.0 -> H1.
    p = _text_pdf(
        tmp_path,
        b"BT /F1 24 Tf 50 740 Td (Big Heading) Tj ET "
        b"BT /F1 12 Tf 14 TL 50 700 Td (Regular body paragraph text here.) Tj "
        b"(A second line of body text.) ' (A third line of body text.) ' ET",
    )
    nodes = _heuristic_autotag(p)["nodes"]
    tags = {n["text"][:12]: n["tag"] for n in nodes}
    assert tags.get("Big Heading") == "H1"
    assert any(n["tag"] == "P" for n in nodes)


def test_heuristic_multipage_tags_every_page(tmp_path):
    pdf = pikepdf.Pdf.new()
    for i in range(1, 4):
        pdf.add_blank_page(page_size=(612, 792))
        pg = pdf.pages[i - 1]
        pg.obj.Contents = pdf.make_stream(
            f"BT /F1 12 Tf 50 700 Td (Body text on page {i} here.) Tj ET".encode())
        pg.obj.Resources = Dictionary(Font=Dictionary(
            F1=Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica)))
    p = str(tmp_path / "multi.pdf")
    pdf.save(p)
    pdf.close()

    nodes = _heuristic_autotag(p)["nodes"]
    pages = {n["page"] for n in nodes}
    assert pages == {1, 2, 3}
