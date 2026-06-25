"""Tests for heading_check.py."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pikepdf
from pikepdf import Dictionary, Name, Array, String
from app.heading_check import check_headings


def _make_heading_pdf(tmp_path, tags):
    pdf = pikepdf.new()
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    pdf.Root.Lang = String("en")
    page = pikepdf.Page(Dictionary(Type=Name.Page, MediaBox=Array([0,0,612,792])))
    pdf.pages.append(page)
    page_ref = pdf.pages[0].obj
    elems = []
    for i, t in enumerate(tags):
        e = pdf.make_indirect(Dictionary(
            Type=Name.StructElem, S=Name("/"+t),
            Pg=page_ref, Alt=String("H %d" % i)))
        elems.append(e)
    root = pdf.make_indirect(Dictionary(
        Type=Name.StructTreeRoot, K=Array(elems)))
    for e in elems:
        e.P = root
    pdf.Root.StructTreeRoot = root
    path = str(tmp_path / "h.pdf")
    pdf.save(path)
    pdf.close()
    return path


class TestCheckHeadings:
    def test_no_struct_tree_returns_list(self, tmp_pdf):
        assert isinstance(check_headings(tmp_pdf()), list)

    def test_valid_sequence_no_issues(self, tmp_path):
        assert check_headings(_make_heading_pdf(tmp_path, ["H1","H2","H3","H2"])) == []

    def test_skip_level_flagged(self, tmp_path):
        issues = check_headings(_make_heading_pdf(tmp_path, ["H1","H3"]))
        assert len(issues) >= 1
        texts = " ".join(str(v) for i in issues for v in i.values())
        assert any(k in texts for k in ["H3","skip","level"])

    def test_missing_file_returns_empty(self):
        assert check_headings("/no/such/file.pdf") == []

    def test_returns_list_of_dicts(self, tmp_path):
        for i in check_headings(_make_heading_pdf(tmp_path, ["H1","H3"])):
            assert isinstance(i, dict)
