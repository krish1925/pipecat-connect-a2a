import asyncio
import socket

import pytest
import uvicorn
from examples.smoke_server import API_KEY, app, tone

from connect_a2a.simulator import ConnectSimulator


@pytest.fixture
async def server():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.02)
    yield f"ws://127.0.0.1:{port}/a2a"
    srv.should_exit = True
    await task


async def test_smoke_call_is_protocol_clean(server):
    sim = ConnectSimulator(server, api_key=API_KEY, out_rate=16000, realtime=False)
    await sim.connect()
    assert await sim.wait_finish()
    await sim.close(1000)
    assert sim.rec.violations == []
    assert sim.rec.finish["type"] == "COMPLETE"
    assert sim.rec.response_headers["a2a-extensions"].startswith("https://docs.aws.amazon.com")
    assert sim.rec.init_response["id"] == "1"
    assert bytes(sim.rec.audio) == tone(16000)


async def test_wrong_token_rejected(server):
    sim = ConnectSimulator(server, api_key="wrong", realtime=False)
    with pytest.raises(Exception):
        await sim.connect()
