"""The simulator's validator must flag the mistakes that make Connect drop a call."""

import subprocess
import sys

from connect_a2a import builders
from connect_a2a.constants import FinishType
from connect_a2a.simulator import CallRecord, Validator
from connect_a2a.tracing import TraceConfig, TraceContext, TurnRecord, build_turn_otlp

CTX = "c"


def otlp() -> dict:
    return build_turn_otlp(TurnRecord(), TraceContext.from_headers(None), TraceConfig())


def run(frames: list[dict]) -> list[str]:
    rec = CallRecord()
    v = Validator(rec)
    for f in frames:
        v.check(f)
    return [x.rule for x in rec.violations]


def chunk(aid: str, *, append: bool, last: bool, task: str = "t1") -> dict:
    return builders.audio_chunk(
        task_id=task, context_id=CTX, artifact_id=aid, pcm=b"\0\0", append=append, last_chunk=last
    )


def trace(task: str = "t1") -> dict:
    return builders.tracing_span(task_id=task, context_id=CTX, artifact_id="tr", otlp=otlp())


def done(task: str = "t1") -> dict:
    return builders.status_update(task_id=task, context_id=CTX)


def test_clean_turn_has_no_violations():
    assert (
        run(
            [
                chunk("a", append=False, last=False),
                chunk("a", append=True, last=True),
                trace(),
                done(),
            ]
        )
        == []
    )


def test_second_open_audio_artifact():
    assert "one-open-audio-artifact" in run(
        [chunk("a", append=False, last=False), chunk("b", append=False, last=False)]
    )


def test_status_with_unfinished_artifact():
    assert "unfinished-artifact" in run([chunk("a", append=False, last=False), trace(), done()])


def test_missing_trace_before_status():
    assert "trace-before-status" in run([chunk("a", append=False, last=True), done()])


def test_append_without_open_artifact():
    assert "append-flag" in run([chunk("a", append=True, last=False)])


def test_frames_after_finish():
    fin = builders.finish(task_id="t1", context_id=CTX, type=FinishType.COMPLETE)
    assert "after-finish" in run([trace(), fin, chunk("a", append=False, last=True)])


def test_span_missing_required_field():
    bad = otlp()
    del bad["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["endTimeUnixNano"]
    f = builders.tracing_span(task_id="t1", context_id=CTX, artifact_id="tr", otlp=bad)
    assert "trace-fields" in run([f])


def test_connect_a2a_does_not_import_pipecat():
    code = "import sys, connect_a2a, connect_a2a.simulator; print(any(m.startswith('pipecat') for m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
