"""Shared fixtures for the remediation engine test suite.

All fixtures that build PDFs use pikepdf directly so there's no dependency
on OpenDataLoader, veraPDF, or any external subprocess.
"""

from __future__ import annotations

import io
import os
import tempfile

import pytest


@pytest.fixture()
def tmp_pdf(tmp_path):
    """Return a helper that writes a minimal valid pikepdf PDF and returns its path."""
    def _make(pages: int = 1, *, text: str = "Hello world", lang: str = "en") -> str:
        import pikepdf
        from pikepdf import Dictionary, Name, String, Array

        pdf = pikepdf.new()
        pdf.Root.Lang = String(lang)
        pdf.Root.MarkInfo = Dictionary(Marked=True, Suspects=False)
        pdf.Root.ViewerPreferences = Dictionary(DisplayDocTitle=True)

        for _ in range(pages):
            page = pikepdf.Page(
                Dictionary(
                    Type=Name.Page,
                    MediaBox=Array([0, 0, 612, 792]),
                    Resources=Dictionary(
                        Font=Dictionary(
                            F1=Dictionary(
                                Type=Name.Font,
                                Subtype=Name.Type1,
                                BaseFont=Name.Helvetica,
                            )
                        )
                    ),
                    Contents=pikepdf.Stream(
                        pdf,
                        f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET".encode(),
                    ),
                )
            )
            pdf.pages.append(page)

        path = str(tmp_path / "test.pdf")
        pdf.save(path)
        pdf.close()
        return path

    return _make


@pytest.fixture()
def make_table_pdf(tmp_path):
    """Factory building a PDF whose structure tree holds one table.

    rows = list of lists of cell tags, e.g. [["TH","TH"], ["TH","TD"]].
    Cells may also be (tag, colspan, rowspan) tuples. Returns the saved path.
    """
    def _make(rows):
        import pikepdf
        from pikepdf import Array, Dictionary, Name

        pdf = pikepdf.new()
        pdf.add_blank_page(page_size=(612, 792))
        root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
        doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=root))
        table = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Table, P=doc))
        trs = []
        for r in rows:
            tr = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.TR, P=table))
            cells = []
            for spec in r:
                tag, cs, rs = (spec if isinstance(spec, tuple) else (spec, 1, 1))
                d = Dictionary(Type=Name.StructElem, S=Name("/" + tag), P=tr)
                if cs > 1 or rs > 1:
                    d.A = Dictionary(O=Name.Table, ColSpan=cs, RowSpan=rs)
                cells.append(pdf.make_indirect(d))
            tr.K = Array(cells)
            trs.append(tr)
        table.K = Array(trs)
        doc.K = Array([table])
        root.K = Array([doc])
        pdf.Root.StructTreeRoot = root
        pdf.Root.MarkInfo = Dictionary(Marked=True)
        path = str(tmp_path / "table.pdf")
        pdf.save(path)
        pdf.close()
        return path
    return _make


@pytest.fixture()
def simple_manifest():
    """Return a minimal manifest with a few nodes."""
    return {
        "source": {"filename": "test.pdf", "lang": "en", "title": "Test"},
        "nodes": [
            {"tag": "H1", "text": "Introduction", "page": 1,
             "bbox": [50, 700, 400, 720]},
            {"tag": "P",  "text": "This is a paragraph.", "page": 1,
             "bbox": [50, 680, 400, 700]},
            {"tag": "Figure", "alt": "", "page": 1,
             "bbox": [50, 500, 300, 650]},
            {"tag": "P", "text": "Figure 1: A sample chart", "page": 1,
             "bbox": [50, 485, 300, 500]},
        ],
    }
