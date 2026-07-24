"""writeback.py geometry helpers — the affine/text-position math that binds
manifest nodes to content-stream operators by position."""

from __future__ import annotations

from app.writeback import _mat_mul, _apply, _contains, _bbox_area, IDENTITY


def test_identity_apply():
    assert _apply(IDENTITY, 3.0, 4.0) == (3.0, 4.0)


def test_matrix_translation_origin():
    # translate(5, 7) applied to origin -> (5, 7)
    m = _mat_mul((1, 0, 0, 1, 5, 7), IDENTITY)
    assert _apply(m, 0.0, 0.0) == (5.0, 7.0)


def test_matrix_compose_order():
    # scale 2 then translate 10 in x: point (1,0) -> (12, 0)
    scale = (2, 0, 0, 2, 0, 0)
    trans = (1, 0, 0, 1, 10, 0)
    m = _mat_mul(scale, trans)
    assert _apply(m, 1.0, 0.0) == (12.0, 0.0)


def test_contains_within_tolerance():
    b = [10.0, 10.0, 100.0, 50.0]        # l, b, r, t
    assert _contains(b, 55.0, 30.0)      # inside
    assert _contains(b, 9.5, 30.0)       # within default 1.0 tolerance
    assert not _contains(b, 5.0, 30.0)   # clearly outside
    assert not _contains(b, 55.0, 200.0)


def test_bbox_area():
    assert _bbox_area([0.0, 0.0, 10.0, 4.0]) == 40.0
