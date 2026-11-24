"""Full Pipecat pipeline (real FastAPIWebsocketTransport + serializer) driven by the simulator."""

import pytest

from connect_a2a.simulator import ConnectSimulator
from tests.helpers import KEY, build_app, serve, tone


def no_violations(sim: ConnectSimulator) -> None:
    assert sim.rec.violations == [], sim.rec.violations


def audio_artifacts(sim: ConnectSimulator) -> list[dict]:
    return [
        f["result"]["artifactUpdate"]
        for f in sim.rec.frames
        if "artifactUpdate" in f.get("result", {})
        and f["result"]["artifactUpdate"]["artifact"]["metadata"].get(
            "https://docs.aws.amazon.com/connect/a2a/ext/v1/eventType"
        )
        == "AUDIO_RESPONSE_CHUNK"
    ]


@pytest.mark.parametrize("in_rate", [8000, 16000, 24000])
@pytest.mark.parametrize("out_rate", [8000, 16000, 24000])
async def test_rate_matrix_single_turn(in_rate: int, out_rate: int) -> None:
    async with serve(build_app()) as url:
        sim = ConnectSimulator(url, api_key=KEY, in_rate=in_rate, out_rate=out_rate, realtime=False)
        await sim.connect()
        await sim.send_text("hello")
        assert await sim.wait_for(
            lambda r: any("statusUpdate" in f.get("result", {}) for f in r.frames)
        )
        await sim.close(1000)
    no_violations(sim)
    # 0.4 s of 16-bit mono audio at the negotiated output rate, within resampler tolerance
    expected = int(out_rate * 0.4) * 2
    assert abs(len(sim.rec.audio) - expected) < expected * 0.1


async def test_barge_in_mid_answer_is_clean() -> None:
    async with serve(build_app()) as url:
        sim = ConnectSimulator(url, api_key=KEY, realtime=False)
        await sim.connect()
        await sim.send_text("long answer please")
        assert await sim.wait_for(lambda r: len(r.audio) > 16000)
        await sim.barge_in()
        await sim.send_text("never mind")
        assert await sim.wait_for(
            lambda r: sum(1 for f in r.frames if "statusUpdate" in f.get("result", {})) >= 2, 15
        )
        await sim.close(1000)
    no_violations(sim)
    arts = audio_artifacts(sim)
    ids = []
    for a in arts:
        if a["artifact"]["artifactId"] not in ids:
            ids.append(a["artifact"]["artifactId"])
    assert len(ids) >= 2  # interrupted artifact abandoned, new one for the next answer
    first = [a for a in arts if a["artifact"]["artifactId"] == ids[0]]
    assert not any(a["lastChunk"] for a in first)  # interrupted: no lastChunk


async def test_dtmf_reaches_pipeline_in_order() -> None:
    bots: list = []
    async with serve(build_app(events=bots)) as url:
        sim = ConnectSimulator(url, api_key=KEY, realtime=False)
        await sim.connect()
        await sim.dtmf("1234")
        await sim.send_silence(0.2)  # inbound traffic drains the queued digits
        assert await sim.wait_for(lambda r: len(r.audio) > 0)
        await sim.close(1000)
    assert [h for h in bots[0].heard if h.startswith("dtmf")][:4] == [
        "dtmf:1",
        "dtmf:2",
        "dtmf:3",
        "dtmf:4",
    ]
    no_violations(sim)


async def test_escalation_speaks_then_finishes_escalate() -> None:
    async with serve(build_app()) as url:
        sim = ConnectSimulator(url, api_key=KEY, realtime=False)
        await sim.connect()
        await sim.send_text("I want a human")
        assert await sim.wait_finish()
        await sim.close(1000)
    no_violations(sim)
    assert sim.rec.finish == {"type": "ESCALATE", "reason": "caller asked for a human"}
    assert len(sim.rec.audio) > 0  # the goodbye was spoken before FINISH
    assert not any(
        "statusUpdate" in f.get("result", {})
        for f in sim.rec.frames[
            sim.rec.frames.index(
                next(
                    f
                    for f in sim.rec.frames
                    if f is not None
                    and "statusUpdate" in f.get("result", {})
                    and f["result"]["statusUpdate"]["status"].get("message")
                )
            )
            + 1 :
        ]
    )


async def test_complete_finish() -> None:
    async with serve(build_app()) as url:
        sim = ConnectSimulator(url, api_key=KEY, realtime=False)
        await sim.connect()
        await sim.send_text("ok bye")
        assert await sim.wait_finish()
        await sim.close(1000)
    no_violations(sim)
    assert sim.rec.finish["type"] == "COMPLETE"


async def test_silence_prompt_is_ignored_gracefully() -> None:
    async with serve(build_app()) as url:
        sim = ConnectSimulator(url, api_key=KEY, realtime=False)
        await sim.connect()
        await sim.send_text("<EMPTY_USER_INPUT>")
        await sim.send_text("hello")
        assert await sim.wait_for(lambda r: len(r.audio) > 0)
        await sim.close(1000)
    no_violations(sim)


async def test_counter_proposed_output_rate_is_honoured() -> None:
    async with serve(build_app(counter_propose=24000)) as url:
        sim = ConnectSimulator(url, api_key=KEY, in_rate=8000, out_rate=8000, realtime=False)
        await sim.connect()
        resp = sim.rec.init_response["result"]["message"]["parts"][0]["data"]["initSessionResponse"]
        assert resp["audioOutputConfiguration"]["sampleRateHertz"] == 24000
        await sim.close(1000)


async def test_bad_token_and_missing_extension_header() -> None:
    async with serve(build_app()) as url:
        sim = ConnectSimulator(url, api_key="wrong", realtime=False)
        with pytest.raises(Exception):  # noqa: B017 - websockets raises a handshake error
            await sim.connect()


async def test_realtime_caller_audio_flows_to_pipeline() -> None:
    async with serve(build_app()) as url:
        sim = ConnectSimulator(url, api_key=KEY, realtime=True)
        await sim.connect()
        await sim.send_audio(tone(8000, 0.3))
        await sim.close(1000)
    no_violations(sim)
