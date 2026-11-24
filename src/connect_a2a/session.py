"""Per-connection outbound state machine.

Enforces what makes Connect close a call: at most one open audio artifact,
every artifact closed with ``lastChunk`` (except on interruption), a trace
before each terminal status, every turn's task closed, and nothing after FINISH.
Methods return the frames to send, in order; the caller owns the socket.
"""

from typing import Any

from connect_a2a import builders
from connect_a2a.constants import FinishType, InterruptionType, TaskState


class SessionStateError(RuntimeError):
    pass


class AgentSession:
    def __init__(self, context_id: str, *, supported_finish_types: list[str] | None = None) -> None:
        self.context_id = context_id
        self.supported_finish_types = set(supported_finish_types or [])
        self.ready = False
        self.finished = False
        self._turn = 0
        self._task_id: str | None = None
        self._artifact_id: str | None = None
        self._artifact_started = False
        self._held: bytes | None = None  # newest chunk, held so the last one can carry lastChunk
        self._trace_sent = False

    # -- introspection --
    @property
    def task_id(self) -> str | None:
        return self._task_id

    @property
    def audio_open(self) -> bool:
        return self._artifact_id is not None

    @property
    def turn_open(self) -> bool:
        return self._task_id is not None

    def _ensure_turn(self) -> str:
        if self.finished:
            raise SessionStateError("session already finished")
        if not self.ready:
            raise SessionStateError("agent must not speak before CHANNEL_STATE READY")
        if self._task_id is None:
            self._turn += 1
            self._task_id = f"task-{self._turn}"
            self._trace_sent = False
        return self._task_id

    # -- audio --
    def audio(self, pcm: bytes) -> list[dict[str, Any]]:
        """Queue one chunk of response audio (one independently playable chunk)."""
        task = self._ensure_turn()
        out: list[dict[str, Any]] = []
        if self._artifact_id is None:
            self._artifact_id = f"resp-{self._turn}-audio"
            self._artifact_started = False
        if self._held is not None:
            out.append(self._emit(task, self._held, last=False))
        self._held = pcm
        return out

    def _emit(self, task: str, pcm: bytes, *, last: bool) -> dict[str, Any]:
        assert self._artifact_id is not None
        frame = builders.audio_chunk(
            task_id=task,
            context_id=self.context_id,
            artifact_id=self._artifact_id,
            pcm=pcm,
            append=self._artifact_started,
            last_chunk=last,
        )
        self._artifact_started = True
        return frame

    def end_audio(self) -> list[dict[str, Any]]:
        """Close the open audio artifact with lastChunk. No-op if none is open."""
        if self._artifact_id is None or self._task_id is None:
            return []
        out: list[dict[str, Any]] = []
        if self._held is not None:
            out.append(self._emit(self._task_id, self._held, last=True))
        self._held = None
        self._artifact_id = None
        return out

    # -- turn end --
    def end_turn(self, otlp: dict[str, Any]) -> list[dict[str, Any]]:
        """Finish the bot's response: close audio, send the turn's trace, then terminal status."""
        if self._task_id is None:
            return []
        out = self.end_audio()
        out += self._close_task(otlp)
        return out

    def _close_task(self, otlp: dict[str, Any]) -> list[dict[str, Any]]:
        assert self._task_id is not None
        task = self._task_id
        out = [
            builders.tracing_span(
                task_id=task,
                context_id=self.context_id,
                artifact_id=f"trace-{self._turn}",
                otlp=otlp,
            ),
            builders.status_update(
                task_id=task, context_id=self.context_id, state=TaskState.COMPLETED
            ),
        ]
        self._task_id = None
        return out

    def interrupt(self, otlp: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Barge-in: send INTERRUPTION, drop audio with no lastChunk, close the task.

        The next audio starts a new artifact under a new task.
        """
        if self.finished:
            return []
        out = [builders.interruption(self.context_id, type=InterruptionType.USER_AUDIO_INPUT)]
        self._held = None
        self._artifact_id = None
        if self._task_id is not None and otlp is not None:
            out += self._close_task(otlp)
        return out

    def on_remote_interruption(self, otlp: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Connect told us the caller barged in: discard output and close the task, no echo."""
        self._held = None
        self._artifact_id = None
        if self._task_id is not None and otlp is not None:
            return self._close_task(otlp)
        return []

    # -- finish --
    def finish(
        self, type: FinishType, reason: str | None, otlp: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """End the conversation. Closes open audio, then FINISH as the last frame."""
        if self.finished:
            return []
        if self.supported_finish_types and type not in self.supported_finish_types:
            raise SessionStateError(f"finish type {type} not in supported_finish_types")
        out = self.end_audio()
        if self._task_id is None:
            self._ensure_turn()
        assert self._task_id is not None
        task = self._task_id
        if otlp is not None and not self._trace_sent:
            out.append(
                builders.tracing_span(
                    task_id=task,
                    context_id=self.context_id,
                    artifact_id=f"trace-{self._turn}",
                    otlp=otlp,
                )
            )
        out.append(
            builders.finish(task_id=task, context_id=self.context_id, type=type, reason=reason)
        )
        self._task_id = None
        self.finished = True
        return out
