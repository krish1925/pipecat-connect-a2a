"""FRAGMENT reassembly and splitting (WebSocket only, never for audio or streamed text).

A fragmented message is the base64 of the original JSON frame, cut into
``totalParts`` pieces that each ride in their own FRAGMENT message.
"""

import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from connect_a2a.constants import EVENT_TYPE_KEY, EventType, Role
from connect_a2a.events import ProtocolError
from connect_a2a.models import Fragment, JsonRpcResult, Message, Result, data_part, dump


@dataclass
class _Pending:
    total: int
    parts: dict[int, str] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)


class FragmentAssembler:
    """Buffers fragments by ``fragmentId`` until every part has arrived."""

    def __init__(self, *, max_pending: int = 64, ttl_secs: float = 60.0) -> None:
        self._pending: dict[str, _Pending] = {}
        self._max_pending = max_pending
        self._ttl = ttl_secs

    def add(self, fragment: Fragment) -> str | None:
        """Add one part. Returns the reassembled JSON text once complete."""
        if fragment.total_parts < 1 or not 0 <= fragment.part_number < fragment.total_parts:
            raise ProtocolError(
                f"fragment {fragment.fragment_id}: part {fragment.part_number} "
                f"of {fragment.total_parts} out of range"
            )
        self._expire()
        pending = self._pending.get(fragment.fragment_id)
        if pending is None:
            if len(self._pending) >= self._max_pending:
                raise ProtocolError("too many incomplete fragmented messages")
            pending = self._pending[fragment.fragment_id] = _Pending(fragment.total_parts)
        elif pending.total != fragment.total_parts:
            raise ProtocolError(f"fragment {fragment.fragment_id}: totalParts changed")
        pending.parts[fragment.part_number] = fragment.payload
        if len(pending.parts) < pending.total:
            return None
        del self._pending[fragment.fragment_id]
        joined = "".join(pending.parts[i] for i in range(pending.total))
        try:
            return base64.b64decode(joined, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as e:
            raise ProtocolError(f"fragment {fragment.fragment_id}: bad payload: {e}") from e

    def _expire(self) -> None:
        now = time.monotonic()
        for fid in [k for k, p in self._pending.items() if now - p.started > self._ttl]:
            del self._pending[fid]


def split_message(frame: dict[str, Any], *, max_payload_chars: int) -> list[dict[str, Any]]:
    """Split an agent frame into FRAGMENT frames whose payloads fit ``max_payload_chars``.

    Use only for large non-streamed frames such as an oversized TRACING_SPAN.
    """
    if max_payload_chars < 4:
        raise ValueError("max_payload_chars too small")
    encoded = base64.b64encode(json.dumps(frame, separators=(",", ":")).encode()).decode()
    chunks = [encoded[i : i + max_payload_chars] for i in range(0, len(encoded), max_payload_chars)]
    fragment_id = f"f-{uuid.uuid4().hex[:12]}"
    out = []
    for n, chunk in enumerate(chunks):
        frag = Fragment(
            fragment_id=fragment_id,
            part_number=n,
            total_parts=len(chunks),
            payload=chunk,
        )
        msg = Message(
            message_id=f"m-{uuid.uuid4().hex[:12]}",
            role=Role.AGENT,
            parts=[data_part("fragment", frag)],
            metadata={EVENT_TYPE_KEY: EventType.FRAGMENT},
        )
        out.append(dump(JsonRpcResult(id=uuid.uuid4().hex[:8], result=Result(message=msg))))
    return out
