"""Pipeline processor that records each turn and marks the end of bot speech."""

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    LLMTextFrame,
    MetricsFrame,
    OutputTransportMessageFrame,
    TranscriptionFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import LLMUsageMetricsData
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from connect_a2a.tracing import ToolCall, now_ns
from pipecat_connect_a2a.serializer import MSG_END_TURN, AmazonConnectA2AFrameSerializer


class A2ATurnProcessor(FrameProcessor):
    """Place between the TTS and ``transport.output()``.

    - fills the turn's trace record (caller text, spoken text, tool calls,
      tokens, caller-stopped-to-first-audio latency);
    - after each ``TTSStoppedFrame`` queues the marker that makes the
      serializer close the audio artifact, send the trace and end the task.
    """

    def __init__(self, serializer: AmazonConnectA2AFrameSerializer, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._ser = serializer
        self._spoken: list[str] = []
        self._tools: dict[str, ToolCall] = {}

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        turn = self._ser.turn

        if isinstance(frame, VADUserStoppedSpeakingFrame):
            turn.user_stopped_ns = now_ns()
        elif isinstance(frame, TranscriptionFrame):
            turn.add_user_input(frame.text)
        elif isinstance(frame, (TTSTextFrame, LLMTextFrame)) and isinstance(frame, TTSTextFrame):
            self._spoken.append(frame.text)
        elif isinstance(frame, FunctionCallInProgressFrame):
            self._tools[frame.tool_call_id] = ToolCall(
                name=frame.function_name,
                tool_id=frame.tool_call_id,
                arguments=frame.arguments,
                started_ns=now_ns(),
            )
        elif isinstance(frame, FunctionCallResultFrame):
            call = self._tools.pop(frame.tool_call_id, None)
            if call:
                call.result = frame.result
                call.ended_ns = now_ns()
                turn.tools.append(call)
        elif isinstance(frame, MetricsFrame):
            for d in frame.data:
                if isinstance(d, LLMUsageMetricsData):
                    turn.input_tokens += d.value.prompt_tokens
                    turn.output_tokens += d.value.completion_tokens

        if isinstance(frame, TTSStoppedFrame):
            if self._spoken:
                turn.add_output(" ".join(self._spoken))
                self._spoken.clear()
            await self.push_frame(frame, direction)
            logger.debug("a2a: end of bot speech, closing turn")
            await self.push_frame(OutputTransportMessageFrame(message={"a2a": MSG_END_TURN}))
            return
        await self.push_frame(frame, direction)
