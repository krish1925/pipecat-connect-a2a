"""Parse what Connect sends into typed events.

Every inbound frame is a JSON-RPC ``SendMessage`` request. The event type is in
``message.metadata[EVENT_TYPE_KEY]``; caller audio and caller text carry no
event type and are told apart by their part's ``mediaType``.
"""

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from connect_a2a.constants import EMPTY_USER_INPUT, EVENT_TYPE_KEY, EventType, MediaType
from connect_a2a.models import (
    ChannelState,
    DtmfInputComplete,
    ErrorPayload,
    Fragment,
    InitSession,
    Interruption,
    JsonRpcRequest,
    Message,
)


class ProtocolError(ValueError):
    """An inbound frame does not follow the contract."""


@dataclass(frozen=True)
class InboundEvent:
    request_id: str | int | None
    context_id: str | None
    message: Message


@dataclass(frozen=True)
class InitSessionEvent(InboundEvent):
    init: InitSession


@dataclass(frozen=True)
class ChannelStateEvent(InboundEvent):
    state: ChannelState

    @property
    def ready(self) -> bool:
        return self.state.state == "READY"


@dataclass(frozen=True)
class AudioInputEvent(InboundEvent):
    pcm: bytes


@dataclass(frozen=True)
class TextInputEvent(InboundEvent):
    text: str

    @property
    def is_silence(self) -> bool:
        return self.text.strip() == EMPTY_USER_INPUT


@dataclass(frozen=True)
class InterruptionEvent(InboundEvent):
    interruption: Interruption


@dataclass(frozen=True)
class DtmfInputCompleteEvent(InboundEvent):
    dtmf: DtmfInputComplete


@dataclass(frozen=True)
class FragmentEvent(InboundEvent):
    fragment: Fragment


@dataclass(frozen=True)
class ErrorEvent(InboundEvent):
    error: ErrorPayload

    @property
    def fatal(self) -> bool:
        # Timeouts precede a close; 400s about artifacts do too. Malformed-trace
        # 400s do not, but the contract gives no field to tell them apart, so
        # only timeouts are treated as certainly fatal.
        return self.error.error_code == 408


@dataclass(frozen=True)
class UnknownEvent(InboundEvent):
    event_type: str | None
    extra: dict[str, Any] = field(default_factory=dict)


def event_type_of(message: Message) -> str | None:
    if not message.metadata:
        return None
    value = message.metadata.get(EVENT_TYPE_KEY)
    return value if isinstance(value, str) else None


def _data(message: Message, key: str) -> dict[str, Any]:
    for part in message.parts:
        if part.data and key in part.data:
            value = part.data[key]
            if isinstance(value, dict):
                return value
    raise ProtocolError(f"missing data part {key!r}")


def parse_request(raw: str | bytes | dict[str, Any]) -> JsonRpcRequest:
    try:
        obj = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        return JsonRpcRequest.model_validate(obj)
    except (json.JSONDecodeError, ValidationError) as e:
        raise ProtocolError(f"not a JSON-RPC SendMessage request: {e}") from e


def parse_event(raw: str | bytes | dict[str, Any]) -> InboundEvent:
    """Parse one inbound frame. Raises :class:`ProtocolError` on malformed input."""
    req = parse_request(raw)
    msg = req.params.message
    base: dict[str, Any] = {"request_id": req.id, "context_id": msg.context_id, "message": msg}
    etype = event_type_of(msg)

    try:
        match etype:
            case EventType.INIT_SESSION:
                init = InitSession.model_validate(_data(msg, "initSession"))
                return InitSessionEvent(**base, init=init)
            case EventType.CHANNEL_STATE:
                cs = ChannelState.model_validate(_data(msg, "channelState"))
                return ChannelStateEvent(**base, state=cs)
            case EventType.INTERRUPTION:
                it = Interruption.model_validate(_data(msg, "interruption"))
                return InterruptionEvent(**base, interruption=it)
            case EventType.DTMF_INPUT_COMPLETE:
                d = DtmfInputComplete.model_validate(_data(msg, "dtmfInputComplete"))
                return DtmfInputCompleteEvent(**base, dtmf=d)
            case EventType.FRAGMENT:
                f = Fragment.model_validate(_data(msg, "fragment"))
                return FragmentEvent(**base, fragment=f)
            case EventType.ERROR:
                err = ErrorPayload.model_validate(_data(msg, "error"))
                return ErrorEvent(**base, error=err)
    except ValidationError as e:
        raise ProtocolError(f"bad {etype} payload: {e}") from e

    if etype is None:
        for part in msg.parts:
            if part.raw is not None and part.media_type == MediaType.AUDIO:
                try:
                    pcm = base64.b64decode(part.raw, validate=True)
                except binascii.Error as e:
                    raise ProtocolError(f"audio part is not base64: {e}") from e
                return AudioInputEvent(**base, pcm=pcm)
        texts = [p.text for p in msg.parts if p.text is not None]
        if texts:
            return TextInputEvent(**base, text="".join(texts))

    return UnknownEvent(**base, event_type=etype)
