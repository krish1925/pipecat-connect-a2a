# pipecat-connect-a2a

Let any [Pipecat](https://github.com/pipecat-ai/pipecat) voice agent take over a live
[Amazon Connect](https://aws.amazon.com/connect/) call through Connect's native agent-to-agent (A2A)
protocol, with audio streaming both ways. Connect keeps telephony, routing and hand-back; you keep
turn detection, STT, the LLM and TTS.

> **Status: pre-alpha.** The protocol library, simulator and Pipecat serializer are tested against
> AWS's published sample events and a local Connect simulator. **It has not yet taken a real Connect
> call**: that needs an account with third-party AI agents enabled (see Phase 0 below). Measured
> latency numbers will go here once it has.

## What's in the box

| Package | What it is |
|---|---|
| `connect_a2a` | Pipecat-free protocol library: Pydantic models for every event, response builders, the artifact/task state machine, per-turn OTLP trace builder, FRAGMENT reassembly, bearer auth. Also `connect_a2a.simulator`, which plays Connect's side of a call. |
| `pipecat_connect_a2a` | `AmazonConnectA2AFrameSerializer` for `FastAPIWebsocketTransport`, `accept_connect_session()` handshake helper, `A2ATurnProcessor`, and `escalate_to_human` / `end_call` tool handlers. |

## Quick start

```python
serializer, init = await accept_connect_session(websocket, api_key=A2A_API_KEY)
transport = FastAPIWebsocketTransport(
    websocket=websocket,
    params=FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        serializer=serializer,
        audio_out_10ms_chunks=2,  # 20 ms chunks
    ),
)
# pipeline: input -> STT -> user agg -> LLM -> TTS -> A2ATurnProcessor(serializer) -> output
# set PipelineParams(audio_in_sample_rate=serializer.audio_in_sample_rate, ...)
escalate, end_call = make_finish_handlers(serializer)
llm.register_function("escalate_to_human", escalate)  # sends FINISH ESCALATE
```

Full bot: [`examples/bot.py`](examples/bot.py). Run it with `uv run --extra examples uvicorn examples.bot:app`.

## How events map

| Connect | Pipecat |
|---|---|
| `INIT_SESSION` / `CHANNEL_STATE READY` | handled in `accept_connect_session`; rates come from the negotiated audio config |
| caller audio (`audio/lpcm`) | `InputAudioRawFrame` (resampled to the pipeline rate) |
| `INTERRUPTION` | `InterruptionFrame` |
| `DTMF_INPUT_COMPLETE` | one `InputDTMFFrame` per digit |
| bot audio | `AUDIO_RESPONSE_CHUNK` artifact, one per spoken response |
| end of bot speech (`TTSStoppedFrame`) | `lastChunk`, then `TRACING_SPAN`, then terminal `statusUpdate` |
| local barge-in | `INTERRUPTION` to Connect, audio dropped with no `lastChunk`, task closed |
| `EndFrame` | `FINISH` (`COMPLETE`, or `ESCALATE` if a tool asked for it) |

The state machine enforces what makes Connect drop a call: one open audio artifact at a time,
`lastChunk` on every finished response, a trace before every terminal status, nothing after `FINISH`,
and no speech before `READY`. Never close with 1000 to end a call; the library sends `FINISH`.

## Test without Connect

```bash
make check                      # ruff, mypy --strict, pytest (includes simulator runs)
A2A_API_KEY=dev-key uv run uvicorn examples.smoke_server:app --port 8765
```

`connect_a2a.simulator.ConnectSimulator` connects to any A2A server, streams caller PCM, barges in,
presses DTMF, and records protocol violations.

## Phase 0: try it on a real account

`examples/smoke_server.py` is a no-Pipecat server that logs every frame, answers the handshake, plays
a 3-second tone, sends one trace and FINISHes. `infra/setup.sh` creates the KMS key, secret and
`A2A_SERVER` application; the Q in Connect agent, security profile and Lex bot steps are listed at
its end. AWS docs list no allowlist, but ElevenLabs' guide says third-party AI agents must be enabled
for the account and Region, so this is the step that settles access.

## Known gaps

- Reconnect (repeated `INIT_SESSION` on the same `contextId` after codes 1005/1006/1012/1013/1014) is
  not wired into the pipeline yet; a new connection starts a new session.
- The contract does not state a frame-size limit or whether the input rate can be counter-proposed.
- Trace attribute coverage follows the docs' samples; check Connect's informational `ERROR` events on first calls.

Protocol reference: [AWS A2A developer guide](https://docs.aws.amazon.com/connect/latest/devguide/a2a-developer-guide.html),
[sample events](https://docs.aws.amazon.com/connect/latest/devguide/a2a-sample-events.html).
