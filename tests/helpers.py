"""Test servers: a scripted Pipecat bot (no API keys) behind the real transport + serializer."""

import asyncio
import math
import socket
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, WebSocket
from pipecat.frames.frames import (
    EndWorkerFrame,
    Frame,
    InputDTMFFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.workers.runner import WorkerRunner

from connect_a2a import FinishType
from pipecat_connect_a2a import (
    A2ATurnProcessor,
    AmazonConnectA2AFrameSerializer,
    HandshakeError,
    accept_connect_session,
)

KEY = "test-key"
TTS_RATE = 24000


def tone(rate: int, secs: float, hz: int = 440) -> bytes:
    n = int(rate * secs)
    return b"".join(
        struct.pack("<h", int(6000 * math.sin(2 * math.pi * hz * i / rate))) for i in range(n)
    )


class ScriptedBot(FrameProcessor):
    """Stands in for STT + LLM + TTS. Replies by keyword so scenarios are deterministic.

    "long" -> 3 s answer, "human" -> goodbye then ESCALATE, "bye" -> goodbye then COMPLETE,
    anything else (and each DTMF entry) -> a 0.4 s answer.
    """

    def __init__(self, serializer: AmazonConnectA2AFrameSerializer) -> None:
        super().__init__()
        self.ser = serializer
        self.heard: list[str] = []
        self._busy = asyncio.Lock()

    async def _say(self, text: str, secs: float) -> None:
        await self.push_frame(TTSStartedFrame())
        await self.push_frame(TTSTextFrame(text=text, text_type="sentence"))
        pcm = tone(TTS_RATE, secs)
        step = TTS_RATE * 2 // 50
        for i in range(0, len(pcm), step):
            await self.push_frame(
                TTSAudioRawFrame(audio=pcm[i : i + step], sample_rate=TTS_RATE, num_channels=1)
            )
        await self.push_frame(TTSStoppedFrame())

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            text = frame.text.lower()
            self.heard.append(text)
            if "long" in text:
                await self._say("Here is a long answer.", 3.0)
            elif "human" in text:
                await self._say("Transferring you now.", 0.4)
                self.ser.request_finish(FinishType.ESCALATE, "caller asked for a human")
                await self.push_frame(EndWorkerFrame())
            elif "bye" in text:
                await self._say("Goodbye.", 0.4)
                await self.push_frame(EndWorkerFrame())
            else:
                await self._say("Okay.", 0.4)
        elif isinstance(frame, InputDTMFFrame):
            self.heard.append(f"dtmf:{frame.button.value}")
            await self._say("Got your digits.", 0.4)
        else:
            await self.push_frame(frame, direction)


def build_app(
    *, counter_propose: int | None = None, events: list[ScriptedBot] | None = None
) -> FastAPI:
    app = FastAPI()

    @app.websocket("/a2a")
    async def a2a(websocket: WebSocket) -> None:
        try:
            ser, init = await accept_connect_session(
                websocket, api_key=KEY, counter_propose_output_rate=counter_propose
            )
        except HandshakeError:
            return
        transport = FastAPIWebsocketTransport(
            websocket=websocket,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                serializer=ser,
                audio_out_10ms_chunks=2,
            ),
        )
        bot = ScriptedBot(ser)
        if events is not None:
            events.append(bot)
        pipeline = Pipeline([transport.input(), bot, A2ATurnProcessor(ser), transport.output()])
        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=ser.audio_in_sample_rate or 8000,
                audio_out_sample_rate=TTS_RATE,
            ),
        )

        @transport.event_handler("on_client_disconnected")
        async def _gone(_t: object, _ws: object) -> None:
            await worker.cancel()

        await WorkerRunner(handle_sigint=False).run(worker)

    return app


@asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[str]:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.02)
    try:
        yield f"ws://127.0.0.1:{port}/a2a"
    finally:
        srv.should_exit = True
        await asyncio.wait([task], timeout=5)
