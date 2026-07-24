"""manifest.py — OpenDataLoader JSON -> remediation manifest."""

from __future__ import annotations

from app.manifest import build_manifest_from_odl, count_nodes, _tag_for


def test_heading_tag_from_level():
    assert _tag_for({"type": "heading", "heading level": 2}) == "H2"
    # clamps out-of-range levels into H1..H6
    assert _tag_for({"type": "heading", "heading level": 9}) == "H6"
    assert _tag_for({"type": "heading", "heading level": 0}) == "H1"


def test_pdfua_tag_preferred_when_present():
    assert _tag_for({"type": "paragraph", "pdfua_tag": "P"}) == "P"
    assert _tag_for({"type": "image", "pdfua_tag": "Figure"}) == "Figure"


def test_unknown_type_defaults_to_paragraph():
    assert _tag_for({"type": "somethingweird"}) == "P"


def test_build_manifest_maps_document_and_nodes():
    odl = {
        "file name": "doc.pdf",
        "number of pages": 2,
        "title": None,
        "kids": [
            {"type": "heading", "pdfua_tag": "H1", "level": "Doctitle",
             "heading level": 1, "page number": 1,
             "bounding box": [10, 700, 300, 720], "content": "My Title"},
            {"type": "paragraph", "pdfua_tag": "P", "page number": 1,
             "bounding box": [10, 680, 300, 695], "content": "Body text"},
        ],
    }
    m = build_manifest_from_odl(odl, "doc.pdf")
    assert m["source"]["pageCount"] == 2
    assert len(m["nodes"]) == 2
    assert m["nodes"][0]["tag"] == "H1"
    assert m["nodes"][0]["headingLevel"] == 1
    # a Doctitle heading with no Info title becomes the suggested title
    assert m["document"]["suggestedTitle"] == "My Title"


def test_figure_gets_alt_and_decorative_defaults():
    odl = {"number of pages": 1, "kids": [
        {"type": "image", "pdfua_tag": "Figure", "page number": 1,
         "bounding box": [0, 0, 10, 10]},
    ]}
    fig = build_manifest_from_odl(odl, "f.pdf")["nodes"][0]
    assert fig["tag"] == "Figure"
    assert fig["alt"] is None and fig["decorative"] is False


def test_table_children_nested_and_counted():
    odl = {"number of pages": 1, "kids": [{
        "type": "table", "pdfua_tag": "Table", "page number": 1,
        "number of rows": 1, "number of columns": 2,
        "rows": [{"type": "table row", "row number": 1, "cells": [
            {"type": "table cell", "pdfua_tag": "TD", "row number": 1,
             "column number": 1, "kids": [
                 {"type": "paragraph", "pdfua_tag": "P", "content": "a"}]},
            {"type": "table cell", "pdfua_tag": "TD", "row number": 1,
             "column number": 2, "kids": [
                 {"type": "paragraph", "pdfua_tag": "P", "content": "b"}]},
        ]}],
    }]}
    m = build_manifest_from_odl(odl, "t.pdf")
    table = m["nodes"][0]
    assert table["tag"] == "Table"
    tr = table["children"][0]
    assert tr["tag"] == "TR"
    assert [c["tag"] for c in tr["children"]] == ["TD", "TD"]
    # Table + TR + 2 TD + 2 P = 6 nodes
    assert count_nodes(m["nodes"]) == 6
