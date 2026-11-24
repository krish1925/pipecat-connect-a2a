"""Pipecat-free implementation of Amazon Connect's A2A voice protocol."""

from connect_a2a.auth import check_bearer, extension_active, response_headers
from connect_a2a.constants import EVENT_TYPE_KEY, EXTENSION_URI, CloseCode, FinishType
from connect_a2a.events import ProtocolError, parse_event
from connect_a2a.fragments import FragmentAssembler
from connect_a2a.session import AgentSession, SessionStateError
from connect_a2a.tracing import TraceConfig, TraceContext, TurnRecord, build_turn_otlp

__all__ = [
    "EVENT_TYPE_KEY",
    "EXTENSION_URI",
    "AgentSession",
    "CloseCode",
    "FinishType",
    "FragmentAssembler",
    "ProtocolError",
    "SessionStateError",
    "TraceConfig",
    "TraceContext",
    "TurnRecord",
    "build_turn_otlp",
    "check_bearer",
    "extension_active",
    "parse_event",
    "response_headers",
]
