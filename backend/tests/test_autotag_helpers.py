"""Tests for helper functions in autotag.py that don't need OpenDataLoader."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.autotag import _bbox_overlap_ratio, _filter_invisible_elements


class TestBboxOverlapRatio:
    def test_full_overlap(self):
        a = (0, 0, 100, 100)
        b = (0, 0, 100, 100)
        assert _bbox_overlap_ratio(a, b) == 1.0

    def test_no_overlap(self):
        a = (0, 0, 50, 50)
        b = (60, 60, 100, 100)
        assert _bbox_overlap_ratio(a, b) == 0.0

    def test_partial_overlap(self):
        a = (0, 0, 100, 100)
        b = (50, 0, 150, 100)   # 50% horizontal overlap
        ratio = _bbox_overlap_ratio(a, b)
        assert abs(ratio - 0.5) < 1e-9

    def test_b_contains_a_fully(self):
        a = (25, 25, 75, 75)
        b = (0, 0, 100, 100)
        assert _bbox_overlap_ratio(a, b) == 1.0

    def test_touching_edge_is_zero(self):
        a = (0, 0, 50, 50)
        b = (50, 0, 100, 50)   # share edge at x=50
        assert _bbox_overlap_ratio(a, b) == 0.0


class TestFilterInvisibleElements:
    def _odl(self, els):
        return {"kids": els}

    def test_no_invisible_regions_returns_unchanged(self):
        odl = self._odl([{"type": "paragraph", "page": 1,
                           "bounding box": [0, 0, 100, 20], "text": "hi"}])
        out = _filter_invisible_elements(odl, {})
        assert len(out["kids"]) == 1

    def test_drops_element_with_high_overlap(self):
        invisible = {1: [(0.0, 0.0, 100.0, 20.0)]}
        odl = self._odl([{"type": "paragraph", "page": 1,
                           "bounding box": [0, 0, 100, 20]}])
        out = _filter_invisible_elements(odl, invisible)
        assert len(out["kids"]) == 0

    def test_keeps_element_with_low_overlap(self):
        # invisible region covers only top 10% of element
        invisible = {1: [(0.0, 18.0, 100.0, 20.0)]}
        odl = self._odl([{"type": "paragraph", "page": 1,
                           "bounding box": [0, 0, 100, 20]}])
        out = _filter_invisible_elements(odl, invisible)
        assert len(out["kids"]) == 1

    def test_containers_not_dropped(self):
        invisible = {1: [(0.0, 0.0, 100.0, 200.0)]}
        odl = self._odl([{"type": "table", "page": 1,
                           "bounding box": [0, 0, 100, 200], "rows": []}])
        out = _filter_invisible_elements(odl, invisible)
        assert len(out["kids"]) == 1   # tables are not leaf types

    def test_different_page_not_dropped(self):
        invisible = {2: [(0.0, 0.0, 100.0, 20.0)]}
        odl = self._odl([{"type": "paragraph", "page": 1,
                           "bounding box": [0, 0, 100, 20]}])
        out = _filter_invisible_elements(odl, invisible)
        assert len(out["kids"]) == 1
