"""Pipecat FrameSerializer for Amazon Connect's A2A voice protocol.

Use it with ``FastAPIWebsocketTransport`` like the Genesys and Twilio
serializers, but build it with :func:`pipecat_connect_a2a.accept_connect_session`
so the INIT_SESSION handshake finishes before the pipeline starts.

One A2A turn can need several frames (last audio chunk, trace, terminal
status), and ``serialize`` returns a single payload. So the serializer sends
its frames itself through the ``send`` callback, in order, and returns ``None``.
Pair it with :class:`~pipecat_connect_a2a.A2ATurnProcessor`, which feeds it the
turn data and the end-of-speech marker.
"""

import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InputDTMFFrame,
    InterruptionFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.utils.time import time_now_iso8601

from connect_a2a import (
    AgentSession,
    FinishType,
    FragmentAssembler,
    ProtocolError,
    TraceConfig,
    TraceContext,
    TurnRecord,
    build_turn_otlp,
    builders,
    parse_event,
)
from connect_a2a.events import (
    AudioInputEvent,
    DtmfInputCompleteEvent,
    ErrorEvent,
    FragmentEvent,
    InitSessionEvent,
    InterruptionEvent,
    TextInputEvent,
)
from connect_a2a.tracing import now_ns

SendFn = Callable[[str], Awaitable[None]]

#: ``OutputTransportMessageFrame.message["a2a"]`` values the serializer understands.
MSG_END_TURN = "end_turn"


class AmazonConnectA2AFrameSerializer(FrameSerializer):
    """Translate between A2A events and Pipecat frames.

    Event handlers: ``on_dtmf(digits, reason)``, ``on_silence()``,
    ``on_connect_error(error)``, ``on_reinit(init)``.
    """

    class InputParams(FrameSerializer.InputParams):
        """``audio_in/out_sample_rate`` are set from the negotiated session."""

        trace: TraceConfig = TraceConfig()
        #: Wire rate for the caller's audio and the bot's audio (8000, 16000 or 24000).
        audio_in_sample_rate: int | None = 8000
        audio_out_sample_rate: int | None = 16000

        model_config = {"arbitrary_types_allowed": True}

    def __init__(
        self,
        *,
        context_id: str,
        send: SendFn,
        params: "AmazonConnectA2AFrameSerializer.InputParams | None" = None,
        trace_context: TraceContext | None = None,
        supported_finish_types: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        params = params or AmazonConnectA2AFrameSerializer.InputParams()
        super().__init__(params, **kwargs)
        self._params: AmazonConnectA2AFrameSerializer.InputParams = params
        self._send = send
        self._send_lock = asyncio.Lock()
        self.session = AgentSession(context_id, supported_finish_types=supported_finish_types)
        self.session.ready = True  # the handshake helper returns only after READY
        self.trace_context = trace_context or TraceContext.from_headers(None)
        self.turn = TurnRecord()
        self._fragments = FragmentAssembler()
        self._remote_interruption = False
        self._pending_frames: deque[Frame] = deque()
        self._pending_finish: tuple[FinishType, str | None] = (FinishType.COMPLETE, None)
        for name in ("on_dtmf", "on_silence", "on_connect_error", "on_reinit"):
            self._register_event_handler(name)

    # -- public API --
    @property
    def context_id(self) -> str:
        return self.session.context_id

    def request_finish(self, type: FinishType, reason: str | None = None) -> None:
        """Choose how the call ends. Takes effect when the pipeline ends (EndFrame)."""
        self._pending_finish = (type, reason)

    def new_turn(self) -> TurnRecord:
        self.turn = TurnRecord()
        return self.turn

    # -- outbound --
    async def _emit(self, frames: list[dict[str, Any]]) -> None:
        async with self._send_lock:
            for f in frames:
                await self._send(json.dumps(f, separators=(",", ":")))

    def _otlp(self) -> dict[str, Any]:
        return build_turn_otlp(self.turn, self.trace_context, self._params.trace)

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if self.session.finished:
            return None
        if isinstance(frame, (EndFrame, CancelFrame)):
            if isinstance(frame, EndFrame):
                ftype, reason = self._pending_finish
                await self._emit(self.session.finish(ftype, reason, self._otlp()))
            return None
        if isinstance(frame, InterruptionFrame):
            self.turn.interrupted = True
            if self._remote_interruption:
                self._remote_interruption = False
                await self._emit(self.session.on_remote_interruption(self._otlp()))
            else:
                await self._emit(self.session.interrupt(self._otlp()))
            self.new_turn()
            return None
        if isinstance(frame, AudioRawFrame):
            pcm = await self._resample_audio_out(frame)
            if pcm:
                if self.turn.first_audio_ns is None:
                    self.turn.first_audio_ns = now_ns()
                await self._emit(self.session.audio(pcm))
            return None
        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            msg = frame.message
            if isinstance(msg, dict) and msg.get("a2a") == MSG_END_TURN:
                await self._emit(self.session.end_turn(self._otlp()))
                self.new_turn()
        return None

    # -- inbound --
    async def deserialize(self, data: str | bytes) -> Frame | None:
        try:
            event = parse_event(data)
        except ProtocolError as e:
            logger.warning(f"{self}: ignoring malformed frame: {e}")
            return None
        frame = await self._to_frame(event)
        # deserialize() returns one frame per call; extras (DTMF digits) wait here
        # and go out on the next inbound frame, which arrives within ~20 ms.
        if frame is not None:
            self._pending_frames.append(frame)
        return self._pending_frames.popleft() if self._pending_frames else None

    async def _to_frame(self, event: Any) -> Frame | None:
        if isinstance(event, AudioInputEvent):
            pcm = await self._resample_audio_in(event.pcm)
            return InputAudioRawFrame(audio=pcm, sample_rate=self._sample_rate, num_channels=1)
        if isinstance(event, InterruptionEvent):
            self._remote_interruption = True
            return InterruptionFrame()
        if isinstance(event, DtmfInputCompleteEvent):
            await self._call_event_handler(
                "on_dtmf", event.dtmf.input_string, str(event.dtmf.termination_reason)
            )
            frames: list[Frame] = []
            for ch in event.dtmf.input_string:
                try:
                    frames.append(InputDTMFFrame(button=KeypadEntry(ch)))
                except ValueError:
                    logger.warning(f"{self}: ignoring non-keypad DTMF character {ch!r}")
            self._pending_frames.extend(frames)  # drained in order by deserialize()
            return None
        if isinstance(event, TextInputEvent):
            if event.is_silence:
                await self._call_event_handler("on_silence")
                return None
            return TranscriptionFrame(
                text=event.text, user_id="caller", timestamp=time_now_iso8601(), finalized=True
            )
        if isinstance(event, FragmentEvent):
            try:
                whole = self._fragments.add(event.fragment)
            except ProtocolError as e:
                logger.warning(f"{self}: bad fragment: {e}")
                return None
            return await self.deserialize(whole) if whole else None
        if isinstance(event, ErrorEvent):
            logger.warning(
                f"{self}: Connect error {event.error.error_code}: {event.error.error_reason}"
            )
            await self._call_event_handler("on_connect_error", event.error)
            return None
        if isinstance(event, InitSessionEvent):
            # Repeated INIT_SESSION: answer it so Connect is not left waiting.
            await self._emit([builders.init_session_response(event.request_id, self.context_id)])
            await self._call_event_handler("on_reinit", event.init)
            return None
        return None  # CHANNEL_STATE, unknown events
