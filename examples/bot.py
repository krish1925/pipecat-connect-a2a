"""A voice bot that takes over an Amazon Connect call through A2A.

    export A2A_API_KEY=...        # the secret stored in Secrets Manager
    export DEEPGRAM_API_KEY=... OPENAI_API_KEY=... CARTESIA_API_KEY=...
    uv run uvicorn examples.bot:app --port 8765

Expose it with `cloudflared tunnel --url http://localhost:8765` and register the
wss:// URL (ending in /a2a) as the A2A_SERVER application's AccessUrl.
"""

import os

from fastapi import FastAPI, WebSocket
from loguru import logger
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.workers.runner import WorkerRunner

from connect_a2a import TraceConfig
from pipecat_connect_a2a import (
    A2ATurnProcessor,
    AmazonConnectA2AFrameSerializer,
    HandshakeError,
    accept_connect_session,
    make_finish_handlers,
)

app = FastAPI()

SYSTEM_PROMPT = (
    "You are a phone agent for Example Co. Keep answers to one or two short sentences. "
    "If the caller asks for a person, call escalate_to_human. "
    "When the conversation is finished, say goodbye and call end_call."
)


@app.websocket("/a2a")
async def a2a(websocket: WebSocket) -> None:
    try:
        serializer, init = await accept_connect_session(
            websocket,
            api_key=os.environ["A2A_API_KEY"],
            params=AmazonConnectA2AFrameSerializer.InputParams(
                trace=TraceConfig(agent_name="pipecat-bot", model="gpt-4.1")
            ),
        )
    except HandshakeError as e:
        logger.warning(f"rejected connection: {e}")
        return
    in_rate = serializer.audio_in_sample_rate or 8000
    logger.info(
        f"call {serializer.context_id}: caller {in_rate} Hz, bot {serializer.audio_out_sample_rate} Hz"
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            serializer=serializer,
            audio_out_10ms_chunks=2,  # 20 ms chunks: each must play on its own
        ),
    )
    stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"])
    llm = OpenAILLMService(api_key=os.environ["OPENAI_API_KEY"])
    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        settings=CartesiaTTSService.Settings(voice=os.environ.get("CARTESIA_VOICE_ID", "")),
    )

    escalate, end_call = make_finish_handlers(serializer)
    llm.register_function("escalate_to_human", escalate)
    llm.register_function("end_call", end_call)
    tools = ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name="escalate_to_human",
                description="Hand the caller to a human agent. Say a short goodbye first.",
                properties={"reason": {"type": "string"}},
                required=["reason"],
            ),
            FunctionSchema(
                name="end_call",
                description="End the call when the conversation is complete.",
                properties={"reason": {"type": "string"}},
                required=[],
            ),
        ]
    )
    history = [
        {"role": "user" if m.role == "ROLE_USER" else "assistant", "content": p.text}
        for m in init.history
        for p in m.parts
        if p.text
    ]
    context = LLMContext([{"role": "system", "content": SYSTEM_PROMPT}, *history], tools=tools)
    aggregators = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer())
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            aggregators.user(),
            llm,
            tts,
            A2ATurnProcessor(serializer),
            transport.output(),
            aggregators.assistant(),
        ]
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(audio_in_sample_rate=in_rate, audio_out_sample_rate=in_rate),
    )

    @transport.event_handler("on_client_disconnected")
    async def _gone(transport: FastAPIWebsocketTransport, ws: WebSocket) -> None:
        await worker.cancel()

    await WorkerRunner(handle_sigint=False).run(worker)
