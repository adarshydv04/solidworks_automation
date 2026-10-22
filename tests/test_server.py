import pytest
from fakes import FakeOps
from mcp import Client
from mcp.types import ElicitResult

from solidworks_automation import server
from solidworks_automation.backend import LocalBackend


@pytest.fixture
def ops():
    fake = FakeOps()
    server.set_backend(LocalBackend(ops_factory=lambda: fake))
    yield fake
    server.set_backend(None)


def answering(approve: bool):
    async def callback(context, params):
        callback.messages.append(params.message)
        return ElicitResult(action="accept", content={"approve": approve})

    callback.messages = []
    return callback


async def test_lists_tools(ops):
    async with Client(server.mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"sw_status", "sw_connect", "sw_create_sketch", "sw_extrude", "sw_save", "sw_add_mate"} <= names


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_connect_asks_user_before_launching(ops, mode):
    ask = answering(True)
    async with Client(server.mcp, elicitation_callback=ask, mode=mode) as client:
        result = await client.call_tool("sw_connect", {})
    assert not result.is_error, result.content
    assert "start SolidWorks" in ask.messages[0]
    assert ("connect", {"launch": True}) in ops.calls


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_connect_respects_decline(ops, mode):
    async with Client(server.mcp, elicitation_callback=answering(False), mode=mode) as client:
        result = await client.call_tool("sw_connect", {})
    assert result.is_error
    assert "declined" in result.content[0].text
    assert ("connect", {"launch": True}) not in ops.calls


async def test_connect_without_prompt_support_needs_explicit_approval(ops):
    async with Client(server.mcp) as client:
        refused = await client.call_tool("sw_connect", {})
        assert refused.is_error and "user_approved" in refused.content[0].text
        assert ("connect", {"launch": True}) not in ops.calls

        approved = await client.call_tool("sw_connect", {"user_approved": True})
    assert not approved.is_error
    assert ("connect", {"launch": True}) in ops.calls


async def test_connect_attaches_without_prompt_when_running(ops):
    ops.running = True
    ask = answering(True)
    async with Client(server.mcp, elicitation_callback=ask) as client:
        result = await client.call_tool("sw_connect", {})
    assert not result.is_error
    assert ask.messages == []


async def test_sketch_entities_are_validated_and_forwarded(ops):
    entities = [
        {"type": "center_rectangle", "center": [0, 0], "width": 100, "height": 60},
        {"type": "circle", "center": [30, 0], "radius": 5},
    ]
    async with Client(server.mcp) as client:
        result = await client.call_tool("sw_create_sketch", {"plane": "top", "entities": entities})
        bad = await client.call_tool("sw_create_sketch", {"entities": [{"type": "circle", "center": [0, 0]}]})
    assert not result.is_error, result.content
    op, kwargs = ops.calls[-1]
    assert op == "create_sketch" and kwargs["plane"] == "top"
    assert kwargs["entities"][1] == {"type": "circle", "center": [30.0, 0.0], "radius": 5.0}
    assert bad.is_error


async def test_solidworks_errors_reach_the_model(ops):
    async with Client(server.mcp) as client:
        result = await client.call_tool("sw_extrude", {"sketch": "Missing", "depth": 10})
    assert result.is_error
    assert "No sketch named 'Missing'" in result.content[0].text


async def test_blind_cut_requires_depth(ops):
    async with Client(server.mcp) as client:
        result = await client.call_tool("sw_cut_extrude", {"sketch": "Sketch2", "end_condition": "blind"})
    assert result.is_error and "depth is required" in result.content[0].text


async def test_screenshot_returns_image(ops):
    async with Client(server.mcp) as client:
        result = await client.call_tool("sw_screenshot", {"view": "isometric"})
    assert not result.is_error, result.content
    assert result.content[0].type == "image"
    assert result.content[0].mime_type == "image/png"


async def test_overwrite_declined_blocks_save(ops):
    async with Client(server.mcp, elicitation_callback=answering(False)) as client:
        result = await client.call_tool("sw_save", {"path": "bracket.sldprt", "overwrite": True})
    assert result.is_error and "not to overwrite" in result.content[0].text
    assert not any(op == "save" for op, _ in ops.calls)


async def test_save_without_overwrite_does_not_prompt(ops):
    ask = answering(False)
    async with Client(server.mcp, elicitation_callback=ask) as client:
        result = await client.call_tool("sw_save", {"path": "bracket.sldprt"})
    assert not result.is_error, result.content
    assert ask.messages == []
