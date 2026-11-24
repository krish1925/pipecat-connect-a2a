"""Phase 0 throwaway: prove Amazon Connect will talk to you at all.

Logs every frame, checks the bearer token, answers INIT_SESSION, waits for
READY, plays a 3-second tone, sends one trace, then FINISH COMPLETE.
No Pipecat needed.

    A2A_API_KEY=... uv run uvicorn examples.smoke_server:app --port 8765
    cloudflared tunnel --url http://localhost:8765
"""

import asyncio
import json
import math
import os
import struct

from fastapi import FastAPI, WebSocket
from loguru import logger

from connect_a2a import (
    AgentSession,
    FinishType,
    TraceConfig,
    TraceContext,
    TurnRecord,
    build_turn_otlp,
    builders,
    check_bearer,
    parse_event,
)
from connect_a2a.constants import EXTENSION_URI, EXTENSIONS_HEADER
from connect_a2a.events import ChannelStateEvent, InitSessionEvent

app = FastAPI()
API_KEY = os.environ.get("A2A_API_KEY", "dev-key")


def tone(rate: int, secs: float = 3.0, hz: int = 440) -> bytes:
    n = int(rate * secs)
    return b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / rate))) for i in range(n)
    )


@app.websocket("/a2a")
async def a2a(ws: WebSocket) -> None:
    logger.info(
        f"upgrade headers: { {k: v for k, v in ws.headers.items() if k != 'authorization'} }"
    )
    if not check_bearer(dict(ws.headers), API_KEY):
        await ws.close(code=1008)
        return
    await ws.accept(headers=[(EXTENSIONS_HEADER.lower().encode(), EXTENSION_URI.encode())])
    ctx = TraceContext.from_headers(ws.headers.get("traceparent"), ws.headers.get("tracestate"))
    session: AgentSession | None = None
    rate = 16000
    greeted = False
    async for raw in ws.iter_text():
        logger.info(f"<- {raw[:600]}")
        ev = parse_event(raw)
        if isinstance(ev, InitSessionEvent):
            cfg = ev.init.audio_output_configuration
            rate = cfg.sample_rate_hertz if cfg else 16000
            logger.info(f"INIT_SESSION audio in/out: {ev.init.audio_input_configuration} / {cfg}")
            session = AgentSession(
                ev.context_id or "ctx",
                supported_finish_types=[str(t) for t in ev.init.supported_finish_types],
            )
            await ws.send_text(
                json.dumps(builders.init_session_response(ev.request_id, session.context_id))
            )
        elif isinstance(ev, ChannelStateEvent) and ev.ready and session and not greeted:
            greeted = True
            session.ready = True
            pcm = tone(rate)
            step = rate * 2 // 50  # 20 ms
            frames = []
            for i in range(0, len(pcm), step):
                frames += session.audio(pcm[i : i + step])
            rec = TurnRecord()
            rec.add_user_input("(call connected)")
            rec.add_output("smoke test greeting tone")
            otlp = build_turn_otlp(rec, ctx, TraceConfig(agent_name="smoke-test"))
            frames += session.end_audio()
            frames += session.finish(FinishType.COMPLETE, "smoke test done", otlp)
            for f in frames:
                logger.info(f"-> {json.dumps(f)[:200]}")
                await ws.send_text(json.dumps(f))
                await asyncio.sleep(0.02)
