"""Per-turn OTLP/JSON trace data, as Connect requires it.

Connect disables external endpoints that skip it. Each turn sends one
TRACING_SPAN artifact holding:

- a root ``invoke_agent <agent>`` span (kind SERVER) whose parent is the span
  from the ``traceparent`` header, with the caller's input as a message event;
- one ``chat <model>`` span (kind CLIENT) per spoken segment, with the output
  as a message event;
- one ``execute_tool <tool>`` span (kind CLIENT) per tool call, with its input
  and result as message events.
"""

import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

SPAN_KIND_SERVER = 2
SPAN_KIND_CLIENT = 3
STATUS_OK = 1
STATUS_ERROR = 2

MESSAGE_EVENT = "aws.connect.span.message"

_TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    parent_span_id: str | None
    tracestate: str | None = None

    @classmethod
    def from_headers(cls, traceparent: str | None, tracestate: str | None = None) -> "TraceContext":
        """Parse W3C ``traceparent``; start a fresh trace if it is missing or malformed."""
        if traceparent:
            m = _TRACEPARENT.match(traceparent.strip().lower())
            if m and m.group(2) != "0" * 32:
                return cls(
                    trace_id=m.group(2),
                    parent_span_id=m.group(3),
                    tracestate=tracestate,
                )
        return cls(trace_id=secrets.token_hex(16), parent_span_id=None, tracestate=tracestate)


def new_span_id() -> str:
    return secrets.token_hex(8)


def now_ns() -> int:
    return time.time_ns()


def _value(v: Any) -> dict[str, Any]:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}  # OTLP/JSON encodes int64 as a string
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def attributes(values: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _value(v)} for k, v in values.items() if v is not None]


def message_event(
    *,
    role: str,
    participant: str,
    content: str,
    time_ns: int,
    type: str = "TEXT_MESSAGE",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        f"{MESSAGE_EVENT}.role": role,
        f"{MESSAGE_EVENT}.type": type,
        f"{MESSAGE_EVENT}.participant": participant,
        f"{MESSAGE_EVENT}.content": content,
    }
    if extra:
        attrs.update(extra)
    return {
        "name": MESSAGE_EVENT,
        "timeUnixNano": str(time_ns),
        "attributes": attributes(attrs),
    }


@dataclass
class ToolCall:
    name: str
    tool_id: str
    arguments: Any
    started_ns: int
    result: Any = None
    ended_ns: int | None = None
    error: str | None = None


@dataclass
class OutputSegment:
    text: str
    started_ns: int
    ended_ns: int


@dataclass
class TurnRecord:
    """Everything one turn's trace needs. Filled while the turn runs."""

    started_ns: int = field(default_factory=now_ns)
    user_inputs: list[tuple[int, str]] = field(default_factory=list)
    outputs: list[OutputSegment] = field(default_factory=list)
    tools: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    #: When the caller stopped speaking, for the turn latency attribute.
    user_stopped_ns: int | None = None
    #: When the first audio chunk of the response went out.
    first_audio_ns: int | None = None
    interrupted: bool = False
    error: str | None = None

    def add_user_input(self, text: str, at_ns: int | None = None) -> None:
        if text.strip():
            self.user_inputs.append((at_ns or now_ns(), text.strip()))

    def add_output(self, text: str, at_ns: int | None = None) -> None:
        t = at_ns or now_ns()
        if not text.strip():
            return
        start = (
            self.outputs[-1].ended_ns if self.outputs else (self.first_audio_ns or self.started_ns)
        )
        self.outputs.append(OutputSegment(text=text.strip(), started_ns=min(start, t), ended_ns=t))

    @property
    def latency_ms(self) -> float | None:
        if self.user_stopped_ns is None or self.first_audio_ns is None:
            return None
        return max(0.0, (self.first_audio_ns - self.user_stopped_ns) / 1e6)


@dataclass
class TraceConfig:
    agent_name: str = "pipecat-agent"
    service_name: str = "pipecat-agent"
    scope_name: str = "pipecat-connect-a2a"
    scope_version: str = "0.1.0"
    model: str | None = None


def _to_json(v: Any) -> str:
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, default=str)
    except TypeError:
        return str(v)


def build_turn_otlp(
    record: TurnRecord,
    ctx: TraceContext,
    cfg: TraceConfig,
    *,
    ended_ns: int | None = None,
) -> dict[str, Any]:
    """Build the ``otlp`` object of a TRACING_SPAN for one turn."""
    end = max(ended_ns or now_ns(), record.started_ns)
    root_id = new_span_id()

    root_events = [
        message_event(role="input", participant="user", content=text, time_ns=t)
        for t, text in record.user_inputs
    ]
    root_attrs: dict[str, Any] = {
        "gen_ai.agent.name": cfg.agent_name,
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.request.model": cfg.model,
        "gen_ai.usage.input_tokens": record.input_tokens or None,
        "gen_ai.usage.output_tokens": record.output_tokens or None,
        "aws.connect.usage.total_tokens": (record.input_tokens + record.output_tokens) or None,
        "pipecat.turn.interrupted": record.interrupted,
        "pipecat.turn.latency_ms": record.latency_ms,
    }
    if record.error:
        root_attrs["aws.connect.error.type"] = record.error
    root: dict[str, Any] = {
        "traceId": ctx.trace_id,
        "spanId": root_id,
        "name": f"invoke_agent {cfg.agent_name}",
        "kind": SPAN_KIND_SERVER,
        "startTimeUnixNano": str(record.started_ns),
        "endTimeUnixNano": str(end),
        "attributes": attributes(root_attrs),
        "status": {"code": STATUS_ERROR if record.error else STATUS_OK},
    }
    if ctx.parent_span_id:
        root["parentSpanId"] = ctx.parent_span_id
    if root_events:
        root["events"] = root_events
    spans = [root]

    chat_name = f"chat {cfg.model}" if cfg.model else "chat"
    for seg in record.outputs:
        spans.append(
            {
                "traceId": ctx.trace_id,
                "spanId": new_span_id(),
                "parentSpanId": root_id,
                "name": chat_name,
                "kind": SPAN_KIND_CLIENT,
                "startTimeUnixNano": str(seg.started_ns),
                "endTimeUnixNano": str(max(seg.ended_ns, seg.started_ns)),
                "attributes": attributes(
                    {"gen_ai.operation.name": "chat", "gen_ai.request.model": cfg.model}
                ),
                "events": [
                    message_event(
                        role="output",
                        participant="assistant",
                        content=seg.text,
                        time_ns=seg.ended_ns,
                    )
                ],
                "status": {"code": STATUS_OK},
            }
        )

    for tool in record.tools:
        t_end = max(tool.ended_ns or end, tool.started_ns)
        tool_attrs = {
            f"{MESSAGE_EVENT}.tool_id": tool.tool_id,
            f"{MESSAGE_EVENT}.tool_type": "FUNCTION",
        }
        events = [
            message_event(
                role="input",
                participant="assistant",
                content=_to_json(tool.arguments),
                time_ns=tool.started_ns,
                type="TOOL_INPUT",
                extra=tool_attrs,
            ),
            message_event(
                role="output",
                participant="tool",
                content=_to_json(tool.error if tool.error else tool.result),
                time_ns=t_end,
                type="TOOL_RESULT",
                extra=tool_attrs,
            ),
        ]
        spans.append(
            {
                "traceId": ctx.trace_id,
                "spanId": new_span_id(),
                "parentSpanId": root_id,
                "name": f"execute_tool {tool.name}",
                "kind": SPAN_KIND_CLIENT,
                "startTimeUnixNano": str(tool.started_ns),
                "endTimeUnixNano": str(t_end),
                "attributes": attributes(
                    {
                        "gen_ai.operation.name": "execute_tool",
                        "gen_ai.tool.name": tool.name,
                    }
                ),
                "events": events,
                "status": {"code": STATUS_ERROR if tool.error else STATUS_OK},
            }
        )

    return {
        "resourceSpans": [
            {
                "resource": {"attributes": attributes({"service.name": cfg.service_name})},
                "scopeSpans": [
                    {
                        "scope": {"name": cfg.scope_name, "version": cfg.scope_version},
                        "spans": spans,
                    }
                ],
            }
        ]
    }
