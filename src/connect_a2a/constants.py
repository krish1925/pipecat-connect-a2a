"""Fixed values of the Amazon Connect A2A protocol contract.

Source: https://docs.aws.amazon.com/connect/latest/devguide/a2a-developer-guide.html
"""

from enum import IntEnum, StrEnum

#: The Connect A2A extension URI. Sent by Connect in the ``A2A-Extensions``
#: header on the WebSocket upgrade, echoed back by the agent, and listed in
#: ``message.extensions`` on agent-originated messages.
EXTENSION_URI = "https://docs.aws.amazon.com/connect/a2a/ext/v1"

#: Metadata key that carries the event type of a message or artifact.
EVENT_TYPE_KEY = f"{EXTENSION_URI}/eventType"

EXTENSIONS_HEADER = "A2A-Extensions"

#: JSON-RPC method Connect uses for every request it sends.
METHOD_SEND_MESSAGE = "SendMessage"

#: Text Connect sends when the caller has been silent for a long time.
EMPTY_USER_INPUT = "<EMPTY_USER_INPUT>"


class EventType(StrEnum):
    INIT_SESSION = "INIT_SESSION"
    INIT_SESSION_RESPONSE = "INIT_SESSION_RESPONSE"
    CHANNEL_STATE = "CHANNEL_STATE"
    TEXT_RESPONSE_CHUNK = "TEXT_RESPONSE_CHUNK"
    AUDIO_RESPONSE_CHUNK = "AUDIO_RESPONSE_CHUNK"
    INTERRUPTION = "INTERRUPTION"
    DTMF_INPUT_COMPLETE = "DTMF_INPUT_COMPLETE"
    FRAGMENT = "FRAGMENT"
    ERROR = "ERROR"
    FINISH = "FINISH"
    TRACING_SPAN = "TRACING_SPAN"


class MediaType(StrEnum):
    JSON = "application/json"
    TEXT = "text/plain"
    AUDIO = "audio/lpcm"


class Role(StrEnum):
    USER = "ROLE_USER"
    AGENT = "ROLE_AGENT"


class TaskState(StrEnum):
    #: Normal end of a turn. The only state that keeps the collaboration going.
    COMPLETED = "TASK_STATE_COMPLETED"
    #: FAILED, CANCELED and REJECTED end the whole collaboration, not just the turn.
    FAILED = "TASK_STATE_FAILED"
    CANCELED = "TASK_STATE_CANCELED"
    REJECTED = "TASK_STATE_REJECTED"


class FinishType(StrEnum):
    COMPLETE = "COMPLETE"
    ESCALATE = "ESCALATE"


class InterruptionType(StrEnum):
    USER_AUDIO_INPUT = "USER_AUDIO_INPUT"
    DTMF_INPUT = "DTMF_INPUT"
    SYSTEM = "SYSTEM"


class DtmfTerminationReason(StrEnum):
    END_CHARACTER = "END_CHARACTER"
    TIMEOUT = "TIMEOUT"
    MAX_LENGTH = "MAX_LENGTH"


class ErrorCategory(StrEnum):
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    TIMEOUT = "TIMEOUT"
    BAD_REQUEST = "BAD_REQUEST"


class CloseCode(IntEnum):
    """WebSocket close codes with a defined meaning for Connect voice sessions."""

    #: Ends the collaboration with no reconnect. Send FINISH instead to end a call.
    NORMAL = 1000
    #: Connect reconnects once (new connection, repeated INIT_SESSION) on these.
    NO_STATUS = 1005
    ABNORMAL = 1006
    SERVICE_RESTART = 1012
    TRY_AGAIN_LATER = 1013
    BAD_GATEWAY = 1014
    #: Connect closes with this when the reconnect fails or drops again.
    INTERNAL_ERROR = 1011
    #: Used by this library to reject an upgrade with a bad bearer token.
    POLICY_VIOLATION = 1008


#: Close codes after which Connect makes its one reconnect attempt.
RECONNECT_CLOSE_CODES = frozenset(
    {
        CloseCode.NO_STATUS,
        CloseCode.ABNORMAL,
        CloseCode.SERVICE_RESTART,
        CloseCode.TRY_AGAIN_LATER,
        CloseCode.BAD_GATEWAY,
    }
)

#: Sample rates the contract allows, for both directions.
SUPPORTED_SAMPLE_RATES = (8000, 16000, 24000)

#: Bytes per sample of the only allowed encoding (16-bit linear PCM, mono).
BYTES_PER_SAMPLE = 2
