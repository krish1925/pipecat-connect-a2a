import base64
import json

import pytest

from connect_a2a import (
    AgentSession,
    FinishType,
    FragmentAssembler,
    ProtocolError,
    SessionStateError,
    TraceConfig,
    TraceContext,
    TurnRecord,
    build_turn_otlp,
    check_bearer,
    extension_active,
    parse_event,
)
from connect_a2a.events import (
    AudioInputEvent,
    ChannelStateEvent,
    DtmfInputCompleteEvent,
    FragmentEvent,
    InitSessionEvent,
    InterruptionEvent,
    TextInputEvent,
)
from connect_a2a.fragments import split_message
from tests.fixtures import aws_samples as S


def otlp() -> dict:
    return build_turn_otlp(TurnRecord(), TraceContext.from_headers(None), TraceConfig())


def ready_session() -> AgentSession:
    s = AgentSession(S.CTX, supported_finish_types=["COMPLETE", "ESCALATE"])
    s.ready = True
    return s


# --- parsing the AWS samples ---
def test_parse_init_session():
    ev = parse_event(S.INIT_SESSION)
    assert isinstance(ev, InitSessionEvent)
    assert ev.request_id == "1" and ev.context_id == S.CTX
    assert ev.init.audio_input_configuration.sample_rate_hertz == 8000
    assert ev.init.audio_output_configuration.sample_rate_hertz == 16000
    assert ev.init.is_voice and ev.init.history[0].parts[0].text == "I need help with my bill"


def test_parse_ready_audio_interruption_dtmf_silence():
    assert parse_event(S.CHANNEL_READY).ready  # type: ignore[union-attr]
    assert isinstance(parse_event(S.CHANNEL_READY), ChannelStateEvent)
    pcm = b"\x01\x00" * 160
    ev = parse_event(S.audio_input(1, base64.b64encode(pcm).decode()))
    assert isinstance(ev, AudioInputEvent) and ev.pcm == pcm
    assert isinstance(parse_event(S.INTERRUPTION), InterruptionEvent)
    d = parse_event(S.DTMF)
    assert isinstance(d, DtmfInputCompleteEvent) and d.dtmf.input_string == "1234"
    t = parse_event(S.SILENCE)
    assert isinstance(t, TextInputEvent) and t.is_silence


def test_unknown_fields_tolerated_and_garbage_rejected():
    frame = json.loads(json.dumps(S.INIT_SESSION))
    frame["params"]["message"]["parts"][0]["data"]["initSession"]["newField"] = 1
    assert isinstance(parse_event(frame), InitSessionEvent)
    with pytest.raises(ProtocolError):
        parse_event("not json")
    with pytest.raises(ProtocolError):
        parse_event(S.audio_input(1, "!!!notbase64"))


def test_bad_sample_rate_rejected():
    frame = json.loads(json.dumps(S.INIT_SESSION))
    frame["params"]["message"]["parts"][0]["data"]["initSession"]["audioInputConfiguration"][
        "sampleRateHertz"
    ] = 44100
    with pytest.raises(ProtocolError):
        parse_event(frame)


# --- auth ---
def test_auth():
    assert check_bearer({"Authorization": "Bearer k"}, "k")
    assert not check_bearer({"authorization": "Bearer nope"}, "k")
    assert not check_bearer({}, "k")
    assert extension_active({"a2a-extensions": S.EXT})
    assert not extension_active({"a2a-extensions": "other"})


# --- state machine ---
def test_cannot_speak_before_ready():
    s = AgentSession(S.CTX)
    with pytest.raises(SessionStateError):
        s.audio(b"\x00\x00")


def test_audio_artifact_append_and_lastchunk():
    s = ready_session()
    frames = s.audio(b"a") + s.audio(b"b") + s.audio(b"c") + s.end_audio()
    updates = [f["result"]["artifactUpdate"] for f in frames]
    assert [u["append"] for u in updates] == [False, True, True]
    assert [u["lastChunk"] for u in updates] == [False, False, True]
    assert len({u["artifact"]["artifactId"] for u in updates}) == 1
    assert not s.audio_open


def test_end_turn_order_audio_trace_status():
    s = ready_session()
    s.audio(b"a")
    frames = s.end_turn(otlp())
    kinds = [next(iter(f["result"])) for f in frames]
    assert kinds == ["artifactUpdate", "artifactUpdate", "statusUpdate"]
    assert frames[0]["result"]["artifactUpdate"]["lastChunk"] is True
    assert frames[1]["result"]["artifactUpdate"]["artifact"]["metadata"][S.KEY] == "TRACING_SPAN"
    assert frames[2]["result"]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert not s.turn_open


def test_never_two_open_artifacts_and_new_task_per_turn():
    s = ready_session()
    s.audio(b"a")
    t1 = s.task_id
    s.end_turn(otlp())
    s.audio(b"b")
    assert s.task_id != t1
    f = s.end_turn(otlp())
    first = f[0]["result"]["artifactUpdate"]
    assert first["append"] is False  # new artifact, not an append to the old one


def test_barge_in_sends_interruption_without_lastchunk_then_new_artifact():
    s = ready_session()
    s.audio(b"a")
    s.audio(b"b")
    frames = s.interrupt(otlp())
    kinds = [next(iter(f["result"])) for f in frames]
    assert kinds[0] == "message"
    assert frames[0]["result"]["message"]["metadata"][S.KEY] == "INTERRUPTION"
    assert "statusUpdate" in kinds and kinds[-1] == "statusUpdate"
    assert not any(
        f["result"].get("artifactUpdate", {}).get("lastChunk")
        and f["result"]["artifactUpdate"]["artifact"]["metadata"][S.KEY] == "AUDIO_RESPONSE_CHUNK"
        for f in frames
    )
    new = s.audio(b"c") + s.end_audio()
    assert new[0]["result"]["artifactUpdate"]["append"] is False


def test_finish_closes_audio_first_and_is_last():
    s = ready_session()
    s.audio(b"a")
    frames = s.finish(FinishType.ESCALATE, "wants a human", otlp())
    last = frames[-1]["result"]["statusUpdate"]
    msg = last["status"]["message"]
    assert msg["parts"][0]["data"]["finish"] == {"type": "ESCALATE", "reason": "wants a human"}
    assert frames[0]["result"]["artifactUpdate"]["lastChunk"] is True
    assert s.finished and s.finish(FinishType.COMPLETE, None) == []
    with pytest.raises(SessionStateError):
        s.audio(b"x")


def test_unsupported_finish_type_rejected():
    s = AgentSession(S.CTX, supported_finish_types=["COMPLETE"])
    s.ready = True
    with pytest.raises(SessionStateError):
        s.finish(FinishType.ESCALATE, "x")


# --- tracing ---
def test_traceparent_and_span_shape():
    ctx = TraceContext.from_headers("00-550e8400e29b41d4a716446655440000-6ba7b8109dad11d1-01")
    assert ctx.trace_id == "550e8400e29b41d4a716446655440000"
    rec = TurnRecord()
    rec.add_user_input("What is my balance?")
    rec.add_output("Your balance is low.")
    out = build_turn_otlp(rec, ctx, TraceConfig(agent_name="a", model="m"))
    spans = out["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert spans[0]["name"] == "invoke_agent a" and spans[0]["parentSpanId"] == "6ba7b8109dad11d1"
    assert spans[1]["name"] == "chat m" and spans[1]["parentSpanId"] == spans[0]["spanId"]
    for sp in spans:
        assert (
            isinstance(sp["startTimeUnixNano"], str)
            and sp["endTimeUnixNano"] >= sp["startTimeUnixNano"]
        )


def test_bad_traceparent_gets_fresh_trace():
    assert len(TraceContext.from_headers("garbage").trace_id) == 32


# --- fragments ---
def test_fragment_round_trip_out_of_order():
    big = {"hello": "x" * 500}
    frames = split_message(big, max_payload_chars=64)
    assert len(frames) > 1
    asm = FragmentAssembler()
    result = None
    for f in reversed(frames):
        req = {
            "jsonrpc": "2.0",
            "method": "SendMessage",
            "id": f["id"],
            "params": {"message": f["result"]["message"]},
        }
        ev = parse_event(req)
        assert isinstance(ev, FragmentEvent)
        result = asm.add(ev.fragment) or result
    assert json.loads(result) == big
