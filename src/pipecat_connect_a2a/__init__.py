"""Pipecat integration for Amazon Connect A2A voice."""

from pipecat_connect_a2a.handshake import HandshakeError, accept_connect_session
from pipecat_connect_a2a.processor import A2ATurnProcessor
from pipecat_connect_a2a.serializer import AmazonConnectA2AFrameSerializer
from pipecat_connect_a2a.tools import ESCALATE_SCHEMA, make_finish_handlers

__all__ = [
    "ESCALATE_SCHEMA",
    "A2ATurnProcessor",
    "AmazonConnectA2AFrameSerializer",
    "HandshakeError",
    "accept_connect_session",
    "make_finish_handlers",
]
