import threading
from http.server import HTTPServer

import pytest
from fakes import FakeOps

from solidworks_automation.backend import RemoteBackend
from solidworks_automation.bridge import make_handler
from solidworks_automation.com import ComThread, SolidWorksError

TOKEN = "test-token"


@pytest.fixture
def bridge():
    thread = ComThread()
    ops = FakeOps(running=True)
    httpd = HTTPServer(("127.0.0.1", 0), make_handler(TOKEN, thread.submit, ops))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", ops
    httpd.shutdown()
    httpd.server_close()
    thread.shutdown()


async def test_remote_call_round_trip(bridge):
    url, ops = bridge
    result = await RemoteBackend(url, TOKEN).call("extrude", sketch="Sketch1", depth=10)
    assert result == {"feature": "Boss-Extrude1"}
    assert ops.calls[-1][0] == "extrude"


async def test_remote_errors_are_reported(bridge):
    url, _ = bridge
    with pytest.raises(SolidWorksError, match="No sketch named 'Missing'"):
        await RemoteBackend(url, TOKEN).call("extrude", sketch="Missing")


async def test_wrong_token_is_rejected(bridge):
    url, ops = bridge
    with pytest.raises(SolidWorksError, match="Invalid bridge token"):
        await RemoteBackend(url, "wrong").call("status")
    assert ops.calls == []


async def test_unknown_operations_are_rejected(bridge):
    url, _ = bridge
    with pytest.raises(SolidWorksError, match="Unknown operation"):
        await RemoteBackend(url, TOKEN).call("__init__")
