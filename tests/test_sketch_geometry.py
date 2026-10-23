import math

import pytest

from solidworks_automation.sketch_geometry import expand_entities


def close(a, b, tol=1e-9):
    return all(math.isclose(x, y, abs_tol=tol) for x, y in zip(a, b, strict=True))


def test_rectangle_becomes_closed_polyline():
    [poly] = expand_entities([{"type": "rectangle", "corner1": [0, 0], "corner2": [10, 5]}])
    assert poly == {"type": "polyline", "points": [(0, 0), (10, 0), (10, 5), (0, 5)], "closed": True}


def test_rotated_center_rectangle():
    [poly] = expand_entities([{"type": "center_rectangle", "center": [0, 0], "width": 4, "height": 2, "angle": 90}])
    assert close(poly["points"][0], (1, -2))


def test_polygon_circumscribed_radius():
    [poly] = expand_entities([{"type": "polygon", "center": [0, 0], "radius": 10, "sides": 6, "inscribed": False}])
    assert math.isclose(math.hypot(*poly["points"][0]), 10 / math.cos(math.pi / 6))
    assert len(poly["points"]) == 6


def test_slot_is_two_lines_and_two_clockwise_arcs():
    shapes = expand_entities([{"type": "slot", "center1": [0, 0], "center2": [20, 0], "width": 6}])
    assert [s["type"] for s in shapes] == ["line", "arc", "line", "arc"]
    assert shapes[0] == {"type": "line", "start": (0, 3), "end": (20, 3)}
    assert shapes[1]["center"] == (20, 0) and shapes[1]["clockwise"]
    # arcs connect the lines into one closed loop
    assert shapes[1]["start"] == shapes[0]["end"] and shapes[1]["end"] == shapes[2]["start"]
    assert shapes[3]["end"] == shapes[0]["start"]


def test_zero_length_slot_is_a_circle():
    [circle] = expand_entities([{"type": "slot", "center1": [5, 5], "center2": [5, 5], "width": 4}])
    assert circle == {"type": "circle", "center": (5, 5), "radius": 2}


def test_linear_grid_pattern():
    shapes = expand_entities([{
        "type": "circle", "center": [0, 0], "radius": 2,
        "pattern": {"type": "linear", "count": 3, "spacing": [10, 0], "count2": 2, "spacing2": [0, 20]},
    }])
    centers = sorted(s["center"] for s in shapes)
    assert centers == [(0, 0), (0, 20), (10, 0), (10, 20), (20, 0), (20, 20)]


def test_bolt_circle_pattern_full_circle():
    shapes = expand_entities([{
        "type": "circle", "center": [50, 0], "radius": 4,
        "pattern": {"type": "circular", "center": [0, 0], "count": 4},
    }])
    expected = [(50, 0), (0, 50), (-50, 0), (0, -50)]
    for shape, want in zip(shapes, expected, strict=True):
        assert close(shape["center"], want)


def test_partial_arc_pattern_includes_both_ends():
    shapes = expand_entities([{
        "type": "circle", "center": [10, 0], "radius": 1,
        "pattern": {"type": "circular", "center": [0, 0], "count": 3, "total_angle": 180},
    }])
    assert close(shapes[-1]["center"], (-10, 0))


def test_mirror_adds_reflected_copy_and_flips_arcs():
    shapes = expand_entities([{
        "type": "arc", "center": [10, 0], "start": [15, 0], "end": [10, 5], "clockwise": False,
        "mirror_about": [[0, -1], [0, 1]],
    }])
    original, mirrored = shapes
    assert close(mirrored["center"], (-10, 0)) and close(mirrored["start"], (-15, 0))
    assert mirrored["clockwise"] is True and original["clockwise"] is False


def test_pattern_then_mirror():
    shapes = expand_entities([{
        "type": "circle", "center": [10, 10], "radius": 1,
        "pattern": {"type": "linear", "count": 2, "spacing": [0, 10]},
        "mirror_about": [[0, 0], [0, 1]],
    }])
    assert sorted(s["center"] for s in shapes) == [(-10, 10), (-10, 20), (10, 10), (10, 20)]


def test_model_coordinates_mapper_applies_to_points_and_vectors():
    # A fake "top plane" mapping: sketch (x, y) = model (X, -Z), with an offset origin.
    def to_sketch(p):
        return p[0] + 100, -p[2]

    shapes = expand_entities(
        [{"type": "circle", "center": [0, 5, 10], "radius": 2,
          "pattern": {"type": "linear", "count": 2, "spacing": [0, 0, 30]}}],
        to_sketch,
    )
    # spacing is a vector: the origin offset must not be applied to it
    assert [s["center"] for s in shapes] == [(100, -10), (100, -40)]


def test_2d_points_required_without_mapper():
    with pytest.raises(ValueError, match="model_coordinates"):
        expand_entities([{"type": "circle", "center": [0, 0, 0], "radius": 1}])
