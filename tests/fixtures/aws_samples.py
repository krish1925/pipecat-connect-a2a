"""Inbound frames copied from AWS's A2A sample events (docs A.1, A.2b, A.3b, A.7, A.8, A.9)."""

EXT = "https://docs.aws.amazon.com/connect/a2a/ext/v1"
KEY = f"{EXT}/eventType"
CTX = "SESSION-1"


def request(id_: str, parts: list[dict], event: str | None) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "id": id_,
        "params": {
            "message": {
                "messageId": f"m-{id_}",
                "contextId": CTX,
                "role": "ROLE_USER",
                "parts": parts,
                "metadata": {KEY: event} if event else {},
            }
        },
    }


def data(key: str, body: dict) -> dict:
    return {"data": {key: body}, "mediaType": "application/json"}


INIT_SESSION = request(
    "1",
    [
        data(
            "initSession",
            {
                "instanceArn": "arn:aws:connect:us-east-1:000000000000:instance/example",
                "contactArn": "arn:aws:connect:us-east-1:000000000000:instance/example/contact/c1",
                "supportedFinishTypes": ["COMPLETE", "ESCALATE"],
                "subscribeToTracingEvents": True,
                "audioInputConfiguration": {
                    "encoding": "LINEAR_PCM",
                    "sampleRateHertz": 8000,
                    "sampleSizeBits": 16,
                    "channelCount": 1,
                },
                "audioOutputConfiguration": {
                    "encoding": "LINEAR_PCM",
                    "sampleRateHertz": 16000,
                    "sampleSizeBits": 16,
                    "channelCount": 1,
                },
                "history": [
                    {
                        "messageId": "h-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "I need help with my bill", "mediaType": "text/plain"}],
                    }
                ],
            },
        )
    ],
    "INIT_SESSION",
)

CHANNEL_READY = request("cs-1", [data("channelState", {"state": "READY"})], "CHANNEL_STATE")


def audio_input(n: int, b64: str) -> dict:
    return request(f"a-{n}", [{"raw": b64, "mediaType": "audio/lpcm"}], None)


INTERRUPTION = request(
    "req-3",
    [data("interruption", {"type": "USER_AUDIO_INPUT", "reason": "user_speech"})],
    "INTERRUPTION",
)

DTMF = request(
    "req-2",
    [data("dtmfInputComplete", {"inputString": "1234", "terminationReason": "END_CHARACTER"})],
    "DTMF_INPUT_COMPLETE",
)

SILENCE = request("req-s", [{"text": "<EMPTY_USER_INPUT>", "mediaType": "text/plain"}], None)
