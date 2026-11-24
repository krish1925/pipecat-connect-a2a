"""LLM tools for ending the call through Connect."""

from collections.abc import Awaitable, Callable
from typing import Any

from pipecat.frames.frames import EndWorkerFrame
from pipecat.processors.frame_processor import FrameDirection

from connect_a2a import FinishType
from pipecat_connect_a2a.serializer import AmazonConnectA2AFrameSerializer

ESCALATE_SCHEMA: dict[str, Any] = {
    "name": "escalate_to_human",
    "description": "Hand the caller to a human agent. Say a short goodbye first.",
    "properties": {"reason": {"type": "string", "description": "Why the caller needs a human."}},
    "required": ["reason"],
}


def make_finish_handlers(
    serializer: AmazonConnectA2AFrameSerializer,
) -> tuple[Callable[[Any], Awaitable[None]], Callable[[Any], Awaitable[None]]]:
    """Return ``(escalate, complete)`` handlers to pass to ``llm.register_function``.

    Each sets how the call ends, lets the bot finish speaking, then ends the
    pipeline; the serializer sends FINISH on the resulting EndFrame.
    """

    async def _end(params: Any, type: FinishType, reason: str | None) -> None:
        serializer.request_finish(type, reason)
        await params.result_callback({"status": "ok"})
        await params.llm.push_frame(EndWorkerFrame(), FrameDirection.UPSTREAM)

    async def escalate_to_human(params: Any) -> None:
        await _end(params, FinishType.ESCALATE, params.arguments.get("reason"))

    async def end_call(params: Any) -> None:
        await _end(params, FinishType.COMPLETE, params.arguments.get("reason"))

    return escalate_to_human, end_call
