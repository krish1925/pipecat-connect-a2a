"""Builders for every message the agent sends to Connect.

Each returns a plain ``dict`` ready for ``json.dumps``. They are stateless;
:class:`connect_a2a.session.ResponseStateMachine` decides when each may be sent.
"""

import base64
import uuid
from datetime import UTC, datetime
from typing import Any

from connect_a2a.constants import (
    EVENT_TYPE_KEY,
    EXTENSION_URI,
    EventType,
    FinishType,
    InterruptionType,
    MediaType,
    Role,
    TaskState,
)
from connect_a2a.models import (
    Artifact,
    ArtifactUpdate,
    AudioConfiguration,
    ErrorPayload,
    Finish,
    InitSessionResponse,
    Interruption,
    JsonRpcResult,
    Message,
    Part,
    Result,
    StatusUpdate,
    TaskStatus,
    data_part,
    dump,
)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _frame(result: Result, request_id: str | int | None = None) -> dict[str, Any]:
    rid = request_id if request_id is not None else new_id()
    return dump(JsonRpcResult(id=rid, result=result))


def _agent_message(
    context_id: str | None, part: Part, event: EventType, *, extensions: bool = True
) -> Message:
    return Message(
        message_id=new_id("m-"),
        context_id=context_id,
        role=Role.AGENT,
        parts=[part],
        extensions=[EXTENSION_URI] if extensions else None,
        metadata={EVENT_TYPE_KEY: event},
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def init_session_response(
    request_id: str | int | None,
    context_id: str,
    *,
    output_sample_rate: int | None = None,
    status_code: str = "200",
) -> dict[str, Any]:
    """Reply to INIT_SESSION. Echoes the request id. Pass a rate only to counter-propose."""
    body = InitSessionResponse(
        status_code=status_code,
        audio_output_configuration=(
            AudioConfiguration(sample_rate_hertz=output_sample_rate)
            if output_sample_rate is not None
            else None
        ),
    )
    msg = _agent_message(
        context_id,
        data_part("initSessionResponse", body),
        EventType.INIT_SESSION_RESPONSE,
    )
    return _frame(Result(message=msg), request_id)


def _artifact_update(
    *,
    task_id: str,
    context_id: str,
    artifact_id: str,
    part: Part,
    event: EventType,
    append: bool,
    last_chunk: bool,
) -> dict[str, Any]:
    update = ArtifactUpdate(
        task_id=task_id,
        context_id=context_id,
        artifact=Artifact(artifact_id=artifact_id, parts=[part], metadata={EVENT_TYPE_KEY: event}),
        append=append,
        last_chunk=last_chunk,
    )
    return _frame(Result(artifact_update=update))


def audio_chunk(
    *,
    task_id: str,
    context_id: str,
    artifact_id: str,
    pcm: bytes,
    append: bool,
    last_chunk: bool,
) -> dict[str, Any]:
    part = Part(raw=base64.b64encode(pcm).decode("ascii"), media_type=MediaType.AUDIO)
    return _artifact_update(
        task_id=task_id,
        context_id=context_id,
        artifact_id=artifact_id,
        part=part,
        event=EventType.AUDIO_RESPONSE_CHUNK,
        append=append,
        last_chunk=last_chunk,
    )


def text_chunk(
    *,
    task_id: str,
    context_id: str,
    artifact_id: str,
    text: str,
    append: bool,
    last_chunk: bool,
) -> dict[str, Any]:
    return _artifact_update(
        task_id=task_id,
        context_id=context_id,
        artifact_id=artifact_id,
        part=Part(text=text, media_type=MediaType.TEXT),
        event=EventType.TEXT_RESPONSE_CHUNK,
        append=append,
        last_chunk=last_chunk,
    )


def tracing_span(
    *, task_id: str, context_id: str, artifact_id: str, otlp: dict[str, Any]
) -> dict[str, Any]:
    """One TRACING_SPAN artifact holding every span of a turn, sent before the terminal status."""
    return _artifact_update(
        task_id=task_id,
        context_id=context_id,
        artifact_id=artifact_id,
        part=data_part("tracingSpan", {"otlp": otlp}),
        event=EventType.TRACING_SPAN,
        append=False,
        last_chunk=True,
    )


def interruption(
    context_id: str,
    *,
    type: InterruptionType = InterruptionType.USER_AUDIO_INPUT,
    reason: str = "user_speech",
) -> dict[str, Any]:
    msg = _agent_message(
        context_id,
        data_part("interruption", Interruption(type=type, reason=reason)),
        EventType.INTERRUPTION,
    )
    return _frame(Result(message=msg))


def status_update(
    *,
    task_id: str,
    context_id: str,
    state: TaskState = TaskState.COMPLETED,
    message: Message | None = None,
) -> dict[str, Any]:
    """Terminal status for a turn's task. Without a message it is a normal end of turn."""
    update = StatusUpdate(
        task_id=task_id,
        context_id=context_id,
        status=TaskStatus(state=state, timestamp=_now_iso(), message=message),
    )
    return _frame(Result(status_update=update))


def finish(
    *, task_id: str, context_id: str, type: FinishType, reason: str | None = None
) -> dict[str, Any]:
    """FINISH as the terminal status of the last task. Send once; send nothing after it."""
    msg = _agent_message(
        None,
        data_part("finish", Finish(type=type, reason=reason)),
        EventType.FINISH,
        extensions=False,
    )
    return status_update(
        task_id=task_id, context_id=context_id, state=TaskState.COMPLETED, message=msg
    )


def task_failed(
    *,
    task_id: str,
    context_id: str,
    error_code: int,
    error_reason: str,
    error_category: str,
) -> dict[str, Any]:
    """FAILED status with a structured error. Ends the whole collaboration."""
    err = ErrorPayload(
        error_code=error_code, error_reason=error_reason, error_category=error_category
    )
    msg = _agent_message(None, data_part("error", err), EventType.ERROR, extensions=False)
    return status_update(
        task_id=task_id, context_id=context_id, state=TaskState.FAILED, message=msg
    )
