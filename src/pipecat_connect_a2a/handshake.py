"""WebSocket handshake: auth, extension echo, INIT_SESSION, wait for READY."""

import asyncio
import json
from typing import Any

from fastapi import WebSocket
from loguru import logger

from connect_a2a import (
    CloseCode,
    ProtocolError,
    TraceContext,
    builders,
    check_bearer,
    parse_event,
)
from connect_a2a.constants import EXTENSION_URI, EXTENSIONS_HEADER, SUPPORTED_SAMPLE_RATES
from connect_a2a.events import ChannelStateEvent, InitSessionEvent
from connect_a2a.models import InitSession
from pipecat_connect_a2a.serializer import AmazonConnectA2AFrameSerializer


class HandshakeError(Exception):
    pass


async def accept_connect_session(
    websocket: WebSocket,
    *,
    api_key: str,
    params: AmazonConnectA2AFrameSerializer.InputParams | None = None,
    counter_propose_output_rate: int | None = None,
    timeout_secs: float = 10.0,
) -> tuple[AmazonConnectA2AFrameSerializer, InitSession]:
    """Authenticate the upgrade, run the handshake and return a ready serializer.

    Rejects the upgrade (before accepting) on a wrong bearer token. Reads
    INIT_SESSION, answers INIT_SESSION_RESPONSE (echoing the request id), waits
    for CHANNEL_STATE READY, then returns. Pass ``counter_propose_output_rate``
    (8000, 16000 or 24000) to ask Connect to play the bot's audio at that rate.
    """
    headers = dict(websocket.headers)
    if not check_bearer(headers, api_key):
        await websocket.close(code=CloseCode.POLICY_VIOLATION)
        raise HandshakeError("bad or missing bearer token")

    await websocket.accept(headers=[(EXTENSIONS_HEADER.lower().encode(), EXTENSION_URI.encode())])
    trace_ctx = TraceContext.from_headers(headers.get("traceparent"), headers.get("tracestate"))

    async def recv() -> Any:
        return parse_event(await asyncio.wait_for(websocket.receive_text(), timeout_secs))

    try:
        first = await recv()
        if not isinstance(first, InitSessionEvent):
            raise HandshakeError(f"expected INIT_SESSION first, got {type(first).__name__}")
        init = first.init
        if not init.is_voice:
            raise HandshakeError("session has no audio configuration; this server is voice-only")
        if counter_propose_output_rate not in (None, *SUPPORTED_SAMPLE_RATES):
            raise ValueError(f"unsupported output rate {counter_propose_output_rate}")

        out_cfg = init.audio_output_configuration
        assert init.audio_input_configuration is not None
        out_rate = counter_propose_output_rate or (
            out_cfg.sample_rate_hertz
            if out_cfg
            else init.audio_input_configuration.sample_rate_hertz
        )
        context_id = first.context_id or builders.new_id("ctx-")
        await websocket.send_text(
            json.dumps(
                builders.init_session_response(
                    first.request_id, context_id, output_sample_rate=counter_propose_output_rate
                )
            )
        )

        while True:
            ev = await recv()
            if isinstance(ev, ChannelStateEvent) and ev.ready:
                break
            logger.debug(f"a2a handshake: ignoring {type(ev).__name__} before READY")
    except (TimeoutError, ProtocolError) as e:
        await websocket.close(code=CloseCode.INTERNAL_ERROR)
        raise HandshakeError(str(e)) from e

    params = params or AmazonConnectA2AFrameSerializer.InputParams()
    params = params.model_copy(
        update={
            "audio_in_sample_rate": init.audio_input_configuration.sample_rate_hertz,
            "audio_out_sample_rate": out_rate,
        }
    )
    serializer = AmazonConnectA2AFrameSerializer(
        context_id=context_id,
        send=websocket.send_text,
        params=params,
        trace_context=trace_ctx,
        supported_finish_types=[str(t) for t in init.supported_finish_types],
    )
    return serializer, init
