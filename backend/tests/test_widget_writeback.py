"""Integration check for Writeback._tag_widgets against a real pikepdf PDF:
radio widgets collapse to one /Form element, hidden widgets are skipped, and
each remaining widget is nested under a /Form via an /OBJR (PDF/UA §7.18.4)."""

from __future__ import annotations

import pytest

pikepdf = pytest.importorskip("pikepdf")
from pikepdf import Array, Dictionary, Name, String  # noqa: E402

from app.writeback import StructTreeBuilder  # noqa: E402


def _make_form_pdf():
    """One page with: a text field, a 3-button radio group, a hidden text field."""
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    page = pdf.pages[0]

    def widget(**kw):
        return pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget, **kw))

    text = widget(FT=Name.Tx, T=String("fullName"), TU=String("Full Name"),
                  Rect=Array([0, 0, 10, 10]))

    # Radio parent field with three kid widgets sharing it.
    radio_parent = pdf.make_indirect(
        Dictionary(FT=Name.Btn, Ff=(1 << 15), T=String("gender"),
                   TU=String("Gender")))
    r_kids = []
    for i in range(3):
        w = widget(Parent=radio_parent, Rect=Array([0, i * 20, 10, i * 20 + 10]),
                   AS=Name(f"/opt{i}"))
        r_kids.append(w)
    radio_parent.Kids = Array(r_kids)

    hidden = widget(FT=Name.Tx, T=String("secret"), TU=String("Secret"),
                    F=2, Rect=Array([0, 0, 10, 10]))  # /F bit 2 = Hidden

    page.obj.Annots = Array([text] + r_kids + [hidden])
    pdf.Root.AcroForm = Dictionary(Fields=Array([text, radio_parent, hidden]))
    return pdf


def _run_tag_widgets(pdf):
    wb = StructTreeBuilder(pdf, manifest={"nodes": []})
    document = pdf.make_indirect(
        Dictionary(Type=Name.StructElem, S=Name.Document))
    wb.struct_root = pdf.make_indirect(
        Dictionary(Type=Name.StructTreeRoot, K=document))
    wb._parent_tree_nums = []
    wb._annot_next_key = len(pdf.pages)
    wb._tag_widgets()
    return wb, document


def _form_elems(document):
    kids = document.get("/K")
    if kids is None:
        return []
    if not isinstance(kids, Array):
        kids = [kids]
    return [k for k in kids if str(k.get("/S")) == "/Form"]


def test_radio_group_and_hidden_widget_handling():
    pdf = _make_form_pdf()
    wb, document = _run_tag_widgets(pdf)

    forms = _form_elems(document)
    # Two Form elements: the text field + the single radio group. Hidden dropped.
    assert len(forms) == 2

    # 4 widgets tagged (1 text + 3 radio); the hidden one is excluded.
    assert wb.report["widgets_tagged"] == 4

    by_label = {str(f.get("/T")): f for f in forms}
    assert set(by_label) == {"Full Name", "Gender"}

    # Text field -> a single OBJR kid.
    text_k = by_label["Full Name"].get("/K")
    assert str(text_k.get("/Type")) == "/OBJR"

    # Radio group -> an array of three OBJR kids.
    radio_form = by_label["Gender"]
    radio_k = radio_form.get("/K")
    assert isinstance(radio_k, Array)
    assert len(radio_k) == 3
    assert all(str(o.get("/Type")) == "/OBJR" for o in radio_k)

    # A multi-widget Form MUST carry a PrintField Role attribute, else veraPDF
    # 7.18.4-2 fails (a role-less Form may reference only one widget).
    a = radio_form.get("/A")
    assert a is not None
    assert str(a.get("/O")) == "/PrintField"
    assert str(a.get("/Role")) == "/rb"

    # The single-widget text Form needs no such attribute (one OBJR is valid).
    assert by_label["Full Name"].get("/A") is None

    # ParentTree got one (key, elem) pair per tagged widget = 4 entries * 2.
    assert len(wb._parent_tree_nums) == 8


def test_raw_field_name_is_humanised_without_tooltip():
    """When a widget has no /TU, the /Form label is derived from /T and made
    human-readable ("dateOfBirth" -> "Date of Birth")."""
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    page = pdf.pages[0]
    w = pdf.make_indirect(Dictionary(
        Type=Name.Annot, Subtype=Name.Widget, FT=Name.Tx,
        T=String("dateOfBirth"), Rect=Array([0, 0, 10, 10])))
    page.obj.Annots = Array([w])
    pdf.Root.AcroForm = Dictionary(Fields=Array([w]))

    _wb, document = _run_tag_widgets(pdf)
    forms = _form_elems(document)
    assert len(forms) == 1
    assert str(forms[0].get("/T")) == "Date of Birth"


def test_no_acroform_is_safe():
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    wb, document = _run_tag_widgets(pdf)
    assert _form_elems(document) == []
    assert wb.report["widgets_tagged"] == 0
