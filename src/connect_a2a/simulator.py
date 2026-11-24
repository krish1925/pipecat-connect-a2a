"""A local stand-in for Amazon Connect's side of a voice call.

Connects to an A2A server, sends INIT_SESSION and READY, streams PCM as caller
audio in real time, and can barge in or press DTMF. It checks the agent against
the rules that make Connect drop a call, and records every frame.
"""

import asyncio
import base64
import json
import time
import wave
from dataclasses import dataclass, field
from typing import Any

import websockets

from connect_a2a.constants import EVENT_TYPE_KEY, EXTENSION_URI

CTX = "sim-context-1"


@dataclass
class Violation:
    rule: str
    detail: str


@dataclass
class CallRecord:
    frames: list[dict[str, Any]] = field(default_factory=list)
    times: list[float] = field(default_factory=list)  # monotonic arrival time per frame
    violations: list[Violation] = field(default_factory=list)
    audio: bytearray = field(default_factory=bytearray)
    close_code: int | None = None
    finish: dict[str, Any] | None = None
    init_response: dict[str, Any] | None = None
    response_headers: dict[str, str] = field(default_factory=dict)


class Validator:
    """Checks agent frames against the contract's call-breaking rules."""

    def __init__(self, rec: CallRecord) -> None:
        self.rec = rec
        self.open_audio: str | None = None
        self.open_task: str | None = None
        self.trace_sent_for: set[str] = set()
        self.finished = False

    def bad(self, rule: str, detail: str) -> None:
        self.rec.violations.append(Violation(rule, detail))

    def check(self, frame: dict[str, Any]) -> None:
        if self.finished:
            self.bad("after-finish", "frame sent after FINISH")
        res = frame.get("result") or {}
        if "artifactUpdate" in res:
            self._artifact(res["artifactUpdate"])
        elif "statusUpdate" in res:
            self._status(res["statusUpdate"])
        elif "message" in res:
            etype = (res["message"].get("metadata") or {}).get(EVENT_TYPE_KEY)
            if etype == "INTERRUPTION":
                self.open_audio = None  # interrupted artifact is abandoned, no lastChunk

    def _artifact(self, u: dict[str, Any]) -> None:
        art = u["artifact"]
        etype = (art.get("metadata") or {}).get(EVENT_TYPE_KEY)
        self.open_task = u["taskId"]
        if etype == "AUDIO_RESPONSE_CHUNK":
            aid = art["artifactId"]
            if self.open_audio and self.open_audio != aid:
                self.bad("one-open-audio-artifact", f"{aid} opened while {self.open_audio} open")
            if not u["append"] and self.open_audio == aid:
                self.bad("append-flag", f"{aid} restarted with append=false")
            if u["append"] and self.open_audio != aid:
                self.bad("append-flag", f"{aid} appended without being open")
            self.open_audio = None if u["lastChunk"] else aid
            for p in art["parts"]:
                if p.get("mediaType") != "audio/lpcm":
                    self.bad("media-type", f"audio chunk mediaType {p.get('mediaType')}")
                self.rec.audio += base64.b64decode(p["raw"])
        elif etype == "TRACING_SPAN":
            self.trace_sent_for.add(u["taskId"])
            self._check_spans(art)

    def _check_spans(self, art: dict[str, Any]) -> None:
        otlp = art["parts"][0]["data"]["tracingSpan"]["otlp"]
        for rs in otlp["resourceSpans"]:
            for ss in rs["scopeSpans"]:
                for sp in ss["spans"]:
                    for k in ("traceId", "spanId", "name", "startTimeUnixNano", "endTimeUnixNano"):
                        if not sp.get(k):
                            self.bad("trace-fields", f"span missing {k}")

    def _status(self, u: dict[str, Any]) -> None:
        task = u["taskId"]
        if self.open_audio:
            self.bad("unfinished-artifact", f"status for {task} with {self.open_audio} still open")
        msg = u["status"].get("message") or {}
        etype = (msg.get("metadata") or {}).get(EVENT_TYPE_KEY)
        if task not in self.trace_sent_for and etype != "ERROR":
            self.bad("trace-before-status", f"{task} ended without a TRACING_SPAN")
        if etype == "FINISH":
            self.finished = True
            self.rec.finish = msg["parts"][0]["data"]["finish"]
        self.open_task = None


def _msg(id_: str, parts: list[dict[str, Any]], event: str | None) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "SendMessage",
            "id": id_,
            "params": {
                "message": {
                    "messageId": f"m-{id_}",
                    "contextId": CTX,
                    "role": "ROLE_USER",
                    "parts": parts,
                    "metadata": {EVENT_TYPE_KEY: event} if event else {},
                }
            },
        }
    )


def _data(key: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"data": {key: body}, "mediaType": "application/json"}


def read_wav_pcm(path: str, rate: int) -> bytes:
    with wave.open(path, "rb") as w:
        if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (rate, 2, 1):
            raise ValueError(f"{path} must be {rate} Hz, 16-bit, mono")
        return w.readframes(w.getnframes())


class ConnectSimulator:
    def __init__(
        self,
        url: str,
        *,
        api_key: str,
        in_rate: int = 8000,
        out_rate: int = 16000,
        chunk_ms: int = 20,
        realtime: bool = True,
        traceparent: str = "00-550e8400e29b41d4a716446655440000-6ba7b8109dad11d1-01",
    ) -> None:
        self.url, self.api_key = url, api_key
        self.in_rate, self.out_rate, self.chunk_ms = in_rate, out_rate, chunk_ms
        self.realtime = realtime
        self.traceparent = traceparent
        self.rec = CallRecord()
        self._v = Validator(self.rec)
        self._ws: Any = None
        self._n = 0
        self._reader: asyncio.Task[None] | None = None
        self.sent_at: dict[str, float] = {}

    def _id(self) -> str:
        self._n += 1
        return f"sim-{self._n}"

    async def connect(self, *, wait_ready_ack: bool = True) -> None:
        self._ws = await websockets.connect(
            self.url,
            additional_headers={
                "Authorization": f"Bearer {self.api_key}",
                "A2A-Extensions": EXTENSION_URI,
                "traceparent": self.traceparent,
            },
            max_size=None,
        )
        self.rec.response_headers = {k.lower(): v for k, v in self._ws.response.headers.items()}

        def cfg(r: int) -> dict[str, Any]:
            return {
                "encoding": "LINEAR_PCM",
                "sampleRateHertz": r,
                "sampleSizeBits": 16,
                "channelCount": 1,
            }

        await self._ws.send(
            _msg(
                "1",
                [
                    _data(
                        "initSession",
                        {
                            "instanceArn": "arn:aws:connect:us-east-1:000000000000:instance/example",
                            "contactArn": "arn:aws:connect:us-east-1:000000000000:instance/example/contact/c1",
                            "supportedFinishTypes": ["COMPLETE", "ESCALATE"],
                            "subscribeToTracingEvents": True,
                            "audioInputConfiguration": cfg(self.in_rate),
                            "audioOutputConfiguration": cfg(self.out_rate),
                            "history": [],
                        },
                    )
                ],
                "INIT_SESSION",
            )
        )
        resp = json.loads(await asyncio.wait_for(self._ws.recv(), 10))
        self.rec.frames.append(resp)
        self.rec.init_response = resp
        if resp.get("id") != "1":
            self._v.bad("echo-request-id", f"INIT_SESSION_RESPONSE id {resp.get('id')!r} != '1'")
        await self._ws.send(
            _msg("cs-1", [_data("channelState", {"state": "READY"})], "CHANNEL_STATE")
        )
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                frame = json.loads(raw)
                self.rec.frames.append(frame)
                self.rec.times.append(time.monotonic())
                self._v.check(frame)
        except websockets.ConnectionClosed:
            pass
        self.rec.close_code = self._ws.close_code

    async def send_audio(self, pcm: bytes) -> None:
        step = self.in_rate * 2 * self.chunk_ms // 1000
        for i in range(0, len(pcm), step):
            b64 = base64.b64encode(pcm[i : i + step]).decode()
            await self._ws.send(_msg(self._id(), [{"raw": b64, "mediaType": "audio/lpcm"}], None))
            if self.realtime:
                await asyncio.sleep(self.chunk_ms / 1000)

    async def send_text(self, text: str) -> None:
        self.sent_at[text] = time.monotonic()
        await self._ws.send(_msg(self._id(), [{"text": text, "mediaType": "text/plain"}], None))

    async def send_silence(self, secs: float) -> None:
        await self.send_audio(b"\x00\x00" * int(self.in_rate * secs))

    async def barge_in(self) -> None:
        await self._ws.send(
            _msg(
                self._id(),
                [_data("interruption", {"type": "USER_AUDIO_INPUT", "reason": "user_speech"})],
                "INTERRUPTION",
            )
        )

    async def dtmf(self, digits: str, reason: str = "END_CHARACTER") -> None:
        await self._ws.send(
            _msg(
                self._id(),
                [_data("dtmfInputComplete", {"inputString": digits, "terminationReason": reason})],
                "DTMF_INPUT_COMPLETE",
            )
        )

    async def wait_for(self, predicate: Any, timeout: float = 10.0) -> bool:
        async def _poll() -> None:
            while not predicate(self.rec):
                await asyncio.sleep(0.02)

        try:
            await asyncio.wait_for(_poll(), timeout)
            return True
        except TimeoutError:
            return False

    async def wait_finish(self, timeout: float = 15.0) -> bool:
        return await self.wait_for(lambda r: r.finish is not None, timeout)

    async def close(self, code: int = 1012) -> None:
        if self._ws:
            await self._ws.close(code=code)
        if self._reader:
            await asyncio.wait([self._reader], timeout=2)
