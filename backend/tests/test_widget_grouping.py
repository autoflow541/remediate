"""_group_widgets — the pure logic that decides how AcroForm widget
annotations map to /Form structure elements (PDF/UA §7.18.4)."""

from __future__ import annotations

from app.writeback import _group_widgets, _FF_RADIO, _FF_PUSHBUTTON


def _radio(wid, field_id, label="Choice", hidden=False):
    return {"wid": wid, "hidden": hidden, "field_id": field_id,
            "ft": "/Btn", "ff": _FF_RADIO, "label": label}


def test_text_fields_are_each_their_own_group():
    widgets = [
        {"wid": 0, "hidden": False, "field_id": ("a", 0), "ft": "/Tx", "ff": 0, "label": "First"},
        {"wid": 1, "hidden": False, "field_id": ("b", 0), "ft": "/Tx", "ff": 0, "label": "Last"},
    ]
    groups = _group_widgets(widgets)
    assert len(groups) == 2
    assert [g["members"] for g in groups] == [[0], [1]]
    assert all(not g["is_radio"] for g in groups)


def test_radio_widgets_sharing_a_field_collapse_to_one_group():
    # Three radio buttons, one shared parent field -> one Form element.
    widgets = [_radio(0, ("rg", 0)), _radio(1, ("rg", 0)), _radio(2, ("rg", 0))]
    groups = _group_widgets(widgets)
    assert len(groups) == 1
    assert groups[0]["members"] == [0, 1, 2]
    assert groups[0]["is_radio"] is True
    assert groups[0]["label"] == "Choice"


def test_distinct_radio_groups_stay_separate():
    widgets = [_radio(0, ("g1", 0)), _radio(1, ("g2", 0)), _radio(2, ("g1", 0))]
    groups = _group_widgets(widgets)
    assert len(groups) == 2
    assert groups[0]["members"] == [0, 2]   # g1, first-appearance order
    assert groups[1]["members"] == [1]      # g2


def test_hidden_widgets_are_dropped():
    widgets = [
        {"wid": 0, "hidden": True, "field_id": ("a", 0), "ft": "/Tx", "ff": 0, "label": "Ghost"},
        {"wid": 1, "hidden": False, "field_id": ("b", 0), "ft": "/Tx", "ff": 0, "label": "Real"},
    ]
    groups = _group_widgets(widgets)
    assert len(groups) == 1
    assert groups[0]["members"] == [1]


def test_hidden_radio_button_excluded_from_its_group():
    widgets = [_radio(0, ("rg", 0)), _radio(1, ("rg", 0), hidden=True), _radio(2, ("rg", 0))]
    groups = _group_widgets(widgets)
    assert len(groups) == 1
    assert groups[0]["members"] == [0, 2]


def test_push_button_is_not_grouped_as_radio():
    # A push button shares FT=/Btn but must never be radio-grouped.
    widgets = [
        {"wid": 0, "hidden": False, "field_id": ("submit", 0),
         "ft": "/Btn", "ff": _FF_PUSHBUTTON, "label": "Submit"},
        {"wid": 1, "hidden": False, "field_id": ("reset", 0),
         "ft": "/Btn", "ff": _FF_PUSHBUTTON, "label": "Reset"},
    ]
    groups = _group_widgets(widgets)
    assert len(groups) == 2
    assert all(not g["is_radio"] for g in groups)


def test_missing_label_falls_back():
    widgets = [{"wid": 0, "hidden": False, "field_id": ("a", 0),
                "ft": "/Tx", "ff": 0, "label": ""}]
    groups = _group_widgets(widgets)
    assert groups[0]["label"] == "Form field"


def test_radio_without_field_id_not_merged():
    # Defensive: no field identity -> cannot group, stays separate.
    widgets = [_radio(0, None), _radio(1, None)]
    groups = _group_widgets(widgets)
    assert len(groups) == 2
