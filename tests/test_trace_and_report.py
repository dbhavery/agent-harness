"""Trace-bus persistence and HTML report rendering tests."""

from __future__ import annotations

from pathlib import Path

from agent_harness.report import render_file, render_html
from agent_harness.scenarios import build_harness
from agent_harness.trace import TraceBus, load_events


def test_trace_written_and_roundtrips(clock, trace_dir):
    bus = TraceBus("run-x", clock, trace_dir=trace_dir)
    bus.emit("plan", step_id=0, detail="hello")
    clock.sleep(0.5)
    bus.emit("tool_call", step_id=1, tool="doc_search", args={"query": "hi"})
    bus.close()

    path = Path(trace_dir) / "run-x.jsonl"
    assert path.exists()
    events = load_events(path)
    assert [e.event for e in events] == ["plan", "tool_call"]
    # Timestamps come from the injected clock: second event is 0.5s later.
    assert events[1].ts - events[0].ts == 0.5
    assert events[1].args == {"query": "hi"}


def test_trace_timestamps_are_deterministic(clock, trace_dir):
    # Two identical runs on a FakeClock produce byte-identical traces.
    outputs = []
    for _ in range(2):
        c = clock.__class__(start=0.0)
        orch, plan = build_harness("flaky_recovery", c, trace_dir=trace_dir)
        r = orch.run(plan, run_id="det")
        outputs.append(Path(r.trace_path).read_text(encoding="utf-8"))
    assert outputs[0] == outputs[1]


def test_report_html_is_self_contained(clock, trace_dir, tmp_path):
    orch, plan = build_harness("timeout_then_cache", clock, trace_dir=trace_dir)
    r = orch.run(plan, run_id="rep")
    out = render_file(r.trace_path, tmp_path / "report.html")
    text = out.read_text(encoding="utf-8")
    # No external assets (offline-openable).
    assert "http://" not in text and "https://" not in text
    assert "<style>" in text
    # Failure classification surfaces as a badge in the report.
    assert "timeout" in text


def test_render_html_handles_empty():
    html = render_html([])
    assert "No events" in html
