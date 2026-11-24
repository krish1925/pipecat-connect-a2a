import base64
import json

from pipecat.frames.frames import (
    EndFrame,
    InputAudioRawFrame,
    InputDTMFFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
)

from connect_a2a import FinishType
from connect_a2a.simulator import CallRecord, Validator
from pipecat_connect_a2a import AmazonConnectA2AFrameSerializer
from pipecat_connect_a2a.serializer import MSG_END_TURN
from tests.fixtures import aws_samples as S


async def make():
    sent: list[dict] = []

    async def send(text: str) -> None:
        sent.append(json.loads(text))

    params = AmazonConnectA2AFrameSerializer.InputParams(
        audio_in_sample_rate=8000, audio_out_sample_rate=16000
    )
    ser = AmazonConnectA2AFrameSerializer(
        context_id=S.CTX, send=send, params=params, supported_finish_types=["COMPLETE", "ESCALATE"]
    )
    ser._sample_rate = 16000  # what setup() records from the pipeline
    return ser, sent


def audio(n: int = 320) -> OutputAudioRawFrame:
    return OutputAudioRawFrame(audio=b"\x01\x00" * n, sample_rate=16000, num_channels=1)


async def test_turn_end_to_end_is_valid():
    ser, sent = await make()
    for _ in range(3):
        await ser.serialize(audio())
    await ser.serialize(OutputTransportMessageFrame(message={"a2a": MSG_END_TURN}))
    await ser.serialize(EndFrame())
    rec = CallRecord()
    v = Validator(rec)
    for f in sent:
        v.check(f)
    assert rec.violations == []
    assert rec.finish == {"type": "COMPLETE"}
    assert len(rec.audio) == 3 * 640


async def test_barge_in_then_new_response_is_valid():
    ser, sent = await make()
    await ser.serialize(audio())
    await ser.serialize(audio())
    await ser.serialize(InterruptionFrame())  # local VAD barge-in -> INTERRUPTION to Connect
    await ser.serialize(audio())
    await ser.serialize(OutputTransportMessageFrame(message={"a2a": MSG_END_TURN}))
    rec = CallRecord()
    v = Validator(rec)
    for f in sent:
        v.check(f)
    assert rec.violations == []
    kinds = [next(iter(f["result"])) for f in sent]
    assert "message" in kinds  # the INTERRUPTION


async def test_remote_interruption_is_not_echoed():
    ser, sent = await make()
    await ser.serialize(audio())
    frame = await ser.deserialize(json.dumps(S.INTERRUPTION))
    assert isinstance(frame, InterruptionFrame)
    before = len(sent)
    await ser.serialize(frame)
    new = sent[before:]
    assert all("message" not in f["result"] for f in new)


async def test_escalation_sends_finish_escalate():
    ser, sent = await make()
    await ser.serialize(audio())
    ser.request_finish(FinishType.ESCALATE, "caller asked for a human")
    await ser.serialize(EndFrame())
    last = sent[-1]["result"]["statusUpdate"]["status"]["message"]["parts"][0]["data"]["finish"]
    assert last == {"type": "ESCALATE", "reason": "caller asked for a human"}


async def test_inbound_audio_resampled_and_dtmf_queued():
    ser, _ = await make()
    pcm = b"\x00\x01" * 160  # 20 ms at 8 kHz
    f = await ser.deserialize(json.dumps(S.audio_input(1, base64.b64encode(pcm).decode())))
    assert isinstance(f, InputAudioRawFrame) and f.sample_rate == 16000
    assert 600 <= len(f.audio) <= 680  # ~20 ms at 16 kHz

    first = await ser.deserialize(json.dumps(S.DTMF))  # "1234"
    assert isinstance(first, InputDTMFFrame) and first.button.value == "1"
    rest = [await ser.deserialize(json.dumps(S.SILENCE)) for _ in range(3)]
    assert [r.button.value for r in rest if isinstance(r, InputDTMFFrame)] == ["2", "3", "4"]
