"""Tests for caption_detect.py — Sprint 19."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.caption_detect import detect_captions, _vertical_gap


class TestVerticalGap:
    def test_caption_directly_below(self):
        fig_bbox  = [50, 500, 300, 650]   # y0=500, y1=650
        cap_bbox  = [50, 480, 300, 498]   # just below figure bottom (480-498)
        gap = _vertical_gap(fig_bbox, cap_bbox)
        assert gap <= 20

    def test_caption_far_away(self):
        fig_bbox = [50, 500, 300, 650]
        cap_bbox = [50, 100, 300, 120]
        gap = _vertical_gap(fig_bbox, cap_bbox)
        assert gap > 100

    def test_empty_bbox_returns_inf(self):
        assert _vertical_gap([], [50, 480, 300, 498]) == float("inf")
        assert _vertical_gap([50, 500, 300, 650], []) == float("inf")


class TestDetectCaptions:
    def test_links_nearby_figure_caption(self, simple_manifest):
        manifest, count = detect_captions(simple_manifest)
        assert count == 1
        # Figure node should now have a Caption child
        fig = next(n for n in manifest["nodes"] if n["tag"] == "Figure")
        assert any(c["tag"] == "Caption" for c in fig.get("children", []))
        # Original paragraph should be removed from top-level
        top_tags = [n["tag"] for n in manifest["nodes"]]
        assert top_tags.count("P") == 1   # only the "This is a paragraph." one remains

    def test_no_figures_no_change(self):
        manifest = {"nodes": [{"tag": "P", "text": "Just a paragraph.", "page": 1}]}
        out, count = detect_captions(manifest)
        assert count == 0
        assert len(out["nodes"]) == 1

    def test_no_candidates_no_change(self):
        manifest = {"nodes": [
            {"tag": "Figure", "alt": "", "page": 1, "bbox": [0, 0, 100, 100]},
            {"tag": "P", "text": "Some unrelated text here.", "page": 1,
             "bbox": [0, 0, 100, 20]},
        ]}
        out, count = detect_captions(manifest)
        assert count == 0

    def test_caption_pattern_matching(self):
        patterns = [
            "Figure 1: results",
            "Fig. 2 — comparison",
            "Table 3: summary",
            "Chart 4 shows",
            "Exhibit 1: overview",
        ]
        for text in patterns:
            manifest = {
                "nodes": [
                    {"tag": "Figure", "alt": "", "page": 1,
                     "bbox": [50, 500, 300, 650]},
                    {"tag": "P", "text": text, "page": 1,
                     "bbox": [50, 462, 300, 478]},
                ],
            }
            _, count = detect_captions(manifest)
            assert count == 1, f"Failed to detect caption: {text!r}"

    def test_different_page_not_linked(self):
        manifest = {"nodes": [
            {"tag": "Figure", "alt": "", "page": 1, "bbox": [50, 500, 300, 650]},
            {"tag": "P", "text": "Figure 1: on next page", "page": 2,
             "bbox": [50, 462, 300, 478]},
        ]}
        _, count = detect_captions(manifest)
        assert count == 0
