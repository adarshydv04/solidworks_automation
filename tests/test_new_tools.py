import pytest
from fakes import FakeOps
from mcp import Client

from solidworks_automation import server
from solidworks_automation.backend import LocalBackend
from solidworks_automation.com import SolidWorksError
from solidworks_automation.operations import SolidWorksOps


@pytest.fixture
def ops():
    fake = FakeOps(running=True)
    server.set_backend(LocalBackend(ops_factory=lambda: fake))
    yield fake
    server.set_backend(None)


async def test_new_tools_are_listed(ops):
    async with Client(server.mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"sw_hole", "sw_mirror", "sw_list_entities"} <= names


async def test_sketch_forwards_patterns_and_model_coordinates(ops):
    entity = {
        "type": "circle", "center": [0, 10, 25], "radius": 4,
        "pattern": {"type": "circular", "center": [0, 10, 0], "count": 6},
    }
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "sw_create_sketch", {"face_point": [5, 10, 5], "model_coordinates": True, "entities": [entity]}
        )
    assert not result.is_error, result.content
    _, kwargs = ops.calls[-1]
    assert kwargs["model_coordinates"] is True
    assert kwargs["entities"][0]["pattern"] == {"type": "circular", "center": [0, 10, 0], "count": 6, "total_angle": 360}
    assert "mirror_about" not in kwargs["entities"][0]


async def test_slot_entity_validates(ops):
    async with Client(server.mcp) as client:
        good = await client.call_tool(
            "sw_create_sketch", {"entities": [{"type": "slot", "center1": [0, 0], "center2": [20, 0], "width": 6}]}
        )
        bad = await client.call_tool(
            "sw_create_sketch", {"entities": [{"type": "slot", "center1": [0, 0], "center2": [20, 0]}]}
        )
    assert not good.is_error and bad.is_error


async def test_extrude_forwards_start_offset_and_up_to_next(ops):
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "sw_extrude", {"sketch": "Sketch3", "depth": 5, "end_condition": "up_to_next", "start_offset": -2}
        )
    assert not result.is_error, result.content
    _, kwargs = ops.calls[-1]
    assert kwargs["end_condition"] == "up_to_next" and kwargs["start_offset"] == -2


async def test_screenshot_returns_one_image_per_view(ops):
    async with Client(server.mcp) as client:
        result = await client.call_tool("sw_screenshot", {"views": ["front", "top", "right"]})
    assert not result.is_error, result.content
    assert [c.type for c in result.content] == ["image", "image", "image"]


async def test_model_from_drawing_prompt(ops):
    async with Client(server.mcp) as client:
        prompts = {p.name for p in (await client.list_prompts()).prompts}
        prompt = await client.get_prompt("model_from_drawing", {"units": "inch"})
    assert "model_from_drawing" in prompts
    assert "25.4" in prompt.messages[0].content.text


def test_hole_rejects_face_point_inside_a_hole():
    with pytest.raises(SolidWorksError, match="face_point lies inside a hole"):
        SolidWorksOps().hole(
            None, face_point=[1, 0, 10], positions=[[0, 0, 10]], diameter=8, depth=None, hole_type="simple",
            counterbore_diameter=None, counterbore_depth=None, countersink_diameter=None, countersink_angle=90,
            name=None,
        )
