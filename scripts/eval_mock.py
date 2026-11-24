"""Evaluate the connector against mock calls: protocol cleanliness and response latency.

Runs scripted-bot scenarios through the real Pipecat transport + serializer with the
simulator as Connect, then prints a table and writes eval_report.json. Exit code 1 on
any violation, so CI can gate on it. Latency here excludes STT/LLM/TTS (the scripted bot
answers instantly); it measures what this connector adds.

    uv run python scripts/eval_mock.py [--repeat 5]
"""

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402
from tests.helpers import KEY, build_app, serve  # noqa: E402

from connect_a2a.simulator import ConnectSimulator  # noqa: E402

logger.remove()

EXT_KEY = "https://docs.aws.amazon.com/connect/a2a/ext/v1/eventType"


def first_audio_latency_ms(sim: ConnectSimulator, text: str) -> float | None:
    sent = sim.sent_at.get(text)
    for f, t in zip(sim.rec.frames, sim.rec.times, strict=False):
        au = f.get("result", {}).get("artifactUpdate")
        if au and au["artifact"]["metadata"].get(EXT_KEY) == "AUDIO_RESPONSE_CHUNK" and sent:
            if t >= sent:
                return (t - sent) * 1000
    return None


async def scenario(name: str, url: str, in_rate: int, out_rate: int) -> dict:
    sim = ConnectSimulator(url, api_key=KEY, in_rate=in_rate, out_rate=out_rate, realtime=False)
    await sim.connect()
    latency = None
    if name == "single_turn":
        await sim.send_text("hello")
        await sim.wait_for(lambda r: any("statusUpdate" in f.get("result", {}) for f in r.frames))
        latency = first_audio_latency_ms(sim, "hello")
    elif name == "barge_in":
        await sim.send_text("long answer")
        await sim.wait_for(lambda r: len(r.audio) > 8000)
        await sim.barge_in()
        await sim.send_text("stop")
        await sim.wait_for(
            lambda r: sum("statusUpdate" in f.get("result", {}) for f in r.frames) >= 2, 15
        )
    elif name == "dtmf":
        await sim.dtmf("12#")
        await sim.send_silence(0.2)
        await sim.wait_for(lambda r: len(r.audio) > 0)
    elif name == "silence_then_speech":
        await sim.send_text("<EMPTY_USER_INPUT>")
        await sim.send_text("hello")
        await sim.wait_for(lambda r: len(r.audio) > 0)
    elif name == "escalation":
        await sim.send_text("get me a human")
        await sim.wait_finish()
    elif name == "complete":
        await sim.send_text("ok bye")
        await sim.wait_finish()
    await sim.close(1000)
    return {
        "scenario": name,
        "in_hz": in_rate,
        "out_hz": out_rate,
        "violations": [f"{v.rule}: {v.detail}" for v in sim.rec.violations],
        "finish": sim.rec.finish,
        "audio_bytes": len(sim.rec.audio),
        "latency_ms": latency,
    }


async def main(repeat: int) -> int:
    names = ["single_turn", "barge_in", "dtmf", "silence_then_speech", "escalation", "complete"]
    rates = [(8000, 8000), (8000, 16000), (16000, 24000), (24000, 24000)]
    results = []
    async with serve(build_app()) as url:
        for n in names:
            for i_r, o_r in rates:
                for _ in range(repeat):
                    results.append(await scenario(n, url, i_r, o_r))
    bad = [r for r in results if r["violations"]]
    lat = [r["latency_ms"] for r in results if r["latency_ms"] is not None]
    print(f"{'scenario':22}{'runs':>6}{'violations':>12}")
    for n in names:
        rs = [r for r in results if r["scenario"] == n]
        print(f"{n:22}{len(rs):>6}{sum(bool(r['violations']) for r in rs):>12}")
    if lat:
        lat.sort()
        print(
            f"\nconnector-added latency, text in -> first audio chunk out (ms): "
            f"p50={statistics.median(lat):.0f} p95={lat[int(len(lat) * 0.95) - 1]:.0f} n={len(lat)}"
        )
    Path("eval_report.json").write_text(json.dumps(results, indent=1))
    for r in bad[:5]:
        print("VIOLATION", r["scenario"], r["in_hz"], r["out_hz"], r["violations"])
    print(f"\n{len(results) - len(bad)}/{len(results)} runs clean")
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=3)
    sys.exit(asyncio.run(main(ap.parse_args().repeat)))
