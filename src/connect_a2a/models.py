"""Pydantic models for the Amazon Connect A2A wire format.

Field names follow the contract (camelCase on the wire, snake_case in Python).
Inbound models allow unknown fields so a new Connect field does not break a
live call; :func:`dump` drops ``None`` so outbound messages carry only what
the contract shows.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from connect_a2a.constants import (
    SUPPORTED_SAMPLE_RATES,
    DtmfTerminationReason,
    ErrorCategory,
    FinishType,
    InterruptionType,
    MediaType,
    Role,
    TaskState,
)


class WireModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="allow",
    )


def dump(model: BaseModel) -> dict[str, Any]:
    """Serialize a model the way it goes on the wire."""
    return model.model_dump(by_alias=True, exclude_none=True, mode="json")


# --- Parts, messages, artifacts ---------------------------------------------


class Part(WireModel):
    """One content part. Exactly one of ``text``, ``raw`` or ``data`` is set."""

    text: str | None = None
    raw: str | None = None  # base64
    data: dict[str, Any] | None = None
    media_type: str | None = None


class Message(WireModel):
    message_id: str
    context_id: str | None = None
    task_id: str | None = None
    role: Role | str
    parts: list[Part] = Field(default_factory=list)
    extensions: list[str] | None = None
    metadata: dict[str, Any] | None = None


class Artifact(WireModel):
    artifact_id: str
    parts: list[Part] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


class ArtifactUpdate(WireModel):
    task_id: str
    context_id: str
    artifact: Artifact
    append: bool
    last_chunk: bool


class TaskStatus(WireModel):
    state: TaskState | str
    timestamp: str | None = None
    message: Message | None = None


class StatusUpdate(WireModel):
    task_id: str
    context_id: str
    status: TaskStatus


class SendMessageParams(WireModel):
    message: Message


class JsonRpcRequest(WireModel):
    """A request Connect sends (always ``SendMessage``)."""

    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    id: str | int | None = None
    params: SendMessageParams


class Result(WireModel):
    """Exactly one of the four fields is set."""

    message: Message | None = None
    task: dict[str, Any] | None = None
    status_update: StatusUpdate | None = None
    artifact_update: ArtifactUpdate | None = None


class JsonRpcResult(WireModel):
    """A frame the agent sends: a direct reply or a pushed event."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    result: Result


# --- Data-part payloads -------------------------------------------------------


class AudioConfiguration(WireModel):
    encoding: Literal["LINEAR_PCM"] = "LINEAR_PCM"
    sample_rate_hertz: int
    sample_size_bits: Literal[16] = 16
    channel_count: Literal[1] = 1

    @field_validator("sample_rate_hertz")
    @classmethod
    def _rate(cls, v: int) -> int:
        if v not in SUPPORTED_SAMPLE_RATES:
            raise ValueError(f"sample rate {v} not in {SUPPORTED_SAMPLE_RATES}")
        return v


class ContactContext(WireModel):
    """Sent only when the AI agent's security profile allows the contact control panel."""

    customer_address: str | None = None
    system_address: str | None = None
    channel: str | None = None


class InitSession(WireModel):
    instance_arn: str | None = None
    contact_arn: str | None = None
    supported_finish_types: list[FinishType | str] = Field(default_factory=list)
    subscribe_to_tracing_events: bool = False
    audio_input_configuration: AudioConfiguration | None = None
    audio_output_configuration: AudioConfiguration | None = None
    history: list[Message] = Field(default_factory=list)
    contact_context: ContactContext | None = None

    @property
    def is_voice(self) -> bool:
        return self.audio_input_configuration is not None


class InitSessionResponse(WireModel):
    status_code: str = "200"
    #: Omit to accept Connect's proposal. A counter-proposal is a full re-declaration.
    audio_output_configuration: AudioConfiguration | None = None


class ChannelState(WireModel):
    state: str


class Interruption(WireModel):
    type: InterruptionType | str
    reason: str | None = None


class DtmfInputComplete(WireModel):
    input_string: str
    termination_reason: DtmfTerminationReason | str


class Fragment(WireModel):
    fragment_id: str
    part_number: int
    total_parts: int
    payload: str
    encoding: Literal["base64"] = "base64"


class ErrorPayload(WireModel):
    error_code: int
    error_reason: str
    error_category: ErrorCategory | str


class Finish(WireModel):
    type: FinishType
    reason: str | None = None


def data_part(key: str, payload: BaseModel | dict[str, Any]) -> Part:
    body = dump(payload) if isinstance(payload, BaseModel) else payload
    return Part(data={key: body}, media_type=MediaType.JSON)
