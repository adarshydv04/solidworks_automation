import math

import pytest
from fakes import FakeOps
from mcp import Client

from solidworks_automation import operations, server
from solidworks_automation.backend import LocalBackend
from solidworks_automation.com import SolidWorksError
from solidworks_automation.operations import SolidWorksOps, _step_call
from solidworks_automation.sketch_geometry import expand_entities


def close(a, b, tol=1e-9):
    return all(math.isclose(x, y, abs_tol=tol) for x, y in zip(a, b, strict=True))


# --- rounded and chamfered corners ---------------------------------------------------


def test_rounded_rectangle_has_four_lines_and_four_tangent_arcs():
    shapes = expand_entities([{"type": "rectangle", "corner1": [0, 0], "corner2": [40, 20], "corner_radius": 5}])
    lines = [s for s in shapes if s["type"] == "line"]
    arcs = [s for s in shapes if s["type"] == "arc"]
    assert len(lines) == 4 and len(arcs) == 4
    corner = next(a for a in arcs if close(a["center"], (5, 5)))
    assert {tuple(round(v, 9) for v in corner["start"]), tuple(round(v, 9) for v in corner["end"])} == {(0, 5), (5, 0)}
    # bottom edge is trimmed by both corner radii
    assert any(close(line["start"], (5, 0)) and close(line["end"], (35, 0)) for line in lines)
    # every arc endpoint lies on its radius
    for arc in arcs:
        assert math.isclose(math.dist(arc["center"], arc["start"]), 5)
        assert math.isclose(math.dist(arc["center"], arc["end"]), 5)


def test_outline_with_arcs_is_closed():
    shapes = expand_entities([{"type": "center_rectangle", "center": [0, 0], "width": 30, "height": 20, "corner_radius": 3}])
    ends = [tuple(round(v, 6) for v in p) for s in shapes for p in (s["start"], s["end"])]
    # each point of a closed loop is shared by exactly two segments
    assert all(ends.count(p) == 2 for p in ends)


def test_per_corner_fillet_and_chamfer():
    shapes = expand_entities([{
        "type": "polyline", "points": [[0, 0], [50, 0], [50, 30], [0, 30]],
        "corners": [None, {"radius": 4}, {"chamfer": 6}, None],
    }])
    assert sum(s["type"] == "arc" for s in shapes) == 1
    chamfer = [s for s in shapes if s["type"] == "line" and close(s["start"], (50, 24))]
    assert chamfer and close(chamfer[0]["end"], (44, 30))


def test_open_polyline_ends_are_never_rounded():
    shapes = expand_entities([{"type": "polyline", "points": [[0, 0], [10, 0], [10, 10]], "closed": False,
                               "corner_radius": 2}])
    assert sum(s["type"] == "arc" for s in shapes) == 1
    assert any(close(s["start"], (0, 0)) for s in shapes if s["type"] == "line")


def test_45_degree_corner_arc_is_tangent():
    # A sloped profile like the 45-degree face in the sample drawing.
    shapes = expand_entities([{"type": "polyline", "points": [[0, 0], [100, 0], [100, 25], [60, 65], [0, 65]],
                               "corners": [None, None, {"radius": 5}, None, None]}])
    [arc] = [s for s in shapes if s["type"] == "arc"]
    # Tangent: the radius to each tangent point is perpendicular to the edge it touches.
    to_start = (arc["start"][0] - arc["center"][0], arc["start"][1] - arc["center"][1])
    assert math.isclose(to_start[1], 0, abs_tol=1e-9)  # vertical edge at x=100 -> horizontal radius
    to_end = (arc["end"][0] - arc["center"][0], arc["end"][1] - arc["center"][1])
    assert math.isclose(to_end[0] * -40 + to_end[1] * 40, 0, abs_tol=1e-9)


def test_radius_too_big_for_segment_is_rejected():
    with pytest.raises(ValueError, match="too large"):
        expand_entities([{"type": "rectangle", "corner1": [0, 0], "corner2": [10, 4], "corner_radius": 3}])


def test_corner_count_must_match_points():
    with pytest.raises(ValueError, match="entries"):
        expand_entities([{"type": "polyline", "points": [[0, 0], [5, 0], [5, 5]], "corners": [None, {"radius": 1}]}])


# --- build plans ------------------------------------------------------------------------


def test_step_call_fills_defaults_and_resolves_refs():
    method, kwargs = _step_call({"op": "cut", "id": "holes", "sketch": "$s2"}, {"s2": "Sketch7"})
    assert method == "extrude"
    assert kwargs["sketch"] == "Sketch7" and kwargs["cut"] is True
    assert kwargs["end_condition"] == "through_all_both"


def test_step_call_flattens_feature_refs_from_hole_steps():
    _, kwargs = _step_call({"op": "mirror", "features": ["$boss", "$holes"], "plane": "right"},
                           {"boss": "Boss-Extrude2", "holes": ["Cut-Extrude1", "Cut-Extrude2"]})
    assert kwargs["features"] == ["Boss-Extrude2", "Cut-Extrude1", "Cut-Extrude2"]


def test_step_call_rejects_unknown_refs_and_missing_depth():
    with pytest.raises(ValueError, match="earlier step"):
        _step_call({"op": "extrude", "sketch": "$nope", "depth": 5}, {})
    with pytest.raises(ValueError, match="depth is required"):
        _step_call({"op": "cut", "sketch": "Sketch1", "end_condition": "blind"}, {})


class ScriptedOps(SolidWorksOps):
    """SolidWorksOps with the SolidWorks-facing parts replaced, to test build orchestration."""

    def __init__(self, fail_at=None):
        super().__init__()
        self.fail_at = fail_at
        self.calls = []
        self.rolled_back = False
        self.counter = 0

    def _part(self, doc):
        return "MODEL"

    def _record_call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        self.counter += 1
        if self.counter == self.fail_at:
            raise SolidWorksError("Extrude failed.")

    def create_sketch(self, **kwargs):
        self._record_call("create_sketch", **kwargs)
        return {"sketch": f"Sketch{self.counter}"}

    def extrude(self, **kwargs):
        self._record_call("extrude", **kwargs)
        return {"feature": f"Feature{self.counter}"}

    def _roll_back(self, model, title, keep):
        self.rolled_back = True
        return ["Feature2", "Sketch1"]

    def rebuild(self, doc):
        return {"rebuilt": True, "problems": []}

    def mass_properties(self, doc):
        return {"bounding_box_mm": {"size": [100, 60, 10]}}


PLAN = [
    {"op": "sketch", "id": "base", "entities": [{"type": "circle", "center": [0, 0], "radius": 5}]},
    {"op": "extrude", "sketch": "$base", "depth": 10},
]


@pytest.fixture
def fake_title(monkeypatch):
    monkeypatch.setattr(operations, "call", lambda obj, name, *args: "Part1")


def test_build_runs_steps_and_passes_references(fake_title):
    ops = ScriptedOps()
    result = ops.build(None, PLAN, rollback_on_error=True)
    assert ops.calls[1] == ("extrude", ops.calls[1][1]) and ops.calls[1][1]["sketch"] == "Sketch1"
    assert [s["step"] for s in result["steps"]] == [1, 2]
    assert result["bounding_box_mm"] == {"size": [100, 60, 10]}


def test_build_rolls_back_and_names_the_failing_step(fake_title):
    ops = ScriptedOps(fail_at=2)
    with pytest.raises(SolidWorksError, match=r"step 2 \(extrude\) failed: Extrude failed\. Rolled back"):
        ops.build(None, PLAN, rollback_on_error=True)
    assert ops.rolled_back


def test_build_can_keep_partial_work(fake_title):
    ops = ScriptedOps(fail_at=2)
    with pytest.raises(SolidWorksError, match="were kept"):
        ops.build(None, PLAN, rollback_on_error=False)
    assert not ops.rolled_back


# --- server schema -------------------------------------------------------------------------


@pytest.fixture
def fake_ops():
    fake = FakeOps(running=True)
    server.set_backend(LocalBackend(ops_factory=lambda: fake))
    yield fake
    server.set_backend(None)


async def test_sw_build_validates_and_forwards_plan(fake_ops):
    plan = [
        {"op": "sketch", "id": "profile", "entities": [
            {"type": "polyline", "points": [[0, 0], [100, 0], [100, 25], [60, 65], [0, 65]],
             "corners": [None, None, {"radius": 5}, None, None]},
        ]},
        {"op": "extrude", "sketch": "$profile", "depth": 66.5, "end_condition": "mid_plane"},
        {"op": "hole", "face_point": [20, 65, 20], "positions": [[30, 65, 0]], "diameter": 17.5},
        {"op": "fillet", "radius": 1, "edges": [[100, 10, 33.25]]},
    ]
    async with Client(server.mcp) as client:
        result = await client.call_tool("sw_build", {"steps": plan})
        bad = await client.call_tool("sw_build", {"steps": [{"op": "extrude", "sketch": "$profile"}]})
    assert not result.is_error, result.content
    op, kwargs = fake_ops.calls[-1]
    assert op == "build" and kwargs["rollback_on_error"] is True
    sent = kwargs["steps"]
    assert sent[0]["entities"][0]["corners"] == [None, None, {"radius": 5.0}, None, None]
    assert sent[1] == {"op": "extrude", "sketch": "$profile", "depth": 66.5, "end_condition": "mid_plane",
                       "reverse": False, "draft_angle": 0.0}
    assert bad.is_error  # extrude without depth
