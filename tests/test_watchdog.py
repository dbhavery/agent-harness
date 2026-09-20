"""The per-step deadline must be a watchdog, not a promise the tool keeps.

Audit finding (2026-09-19): the executor compared the tool's own
``next_latency_ms()`` against the budget and never timed the call. A tool that
declared 10ms and then slept 1.5s returned ``ok`` after 1513ms against a 500ms
deadline, and the trace recorded its latency as 10ms. These tests reproduce that
and pin the fix: the deadline is enforced against measured wall-clock time, and
the measured time is what lands in the trace.

Every tool here reports a latency it has no intention of honouring, which is
exactly the case a self-reported check cannot catch.
"""

from __future__ import annotations

import threading
import time

from agent_harness.classification import FailureClass
from agent_harness.orchestrator import Orchestrator
from agent_harness.retry import RetryPolicy
from agent_harness.schemas import FlakyApiInput, FlakyApiOutput, Plan, ToolStep
from agent_harness.tools.base import Tool


class LyingLatencyTool(Tool[FlakyApiInput, FlakyApiOutput]):
    """Declares one latency and then really blocks for a different one."""

    name = "slow_api"
    input_model = FlakyApiInput
    output_model = FlakyApiOutput

    def __init__(self, *, declared_ms: float, real_sleep_s: float) -> None:
        self._declared_ms = declared_ms
        self._real_sleep_s = real_sleep_s
        self.calls = 0

    def next_latency_ms(self, request: FlakyApiInput, attempt: int = 0) -> float:
        return self._declared_ms

    def run(self, request: FlakyApiInput, attempt: int = 0) -> FlakyApiOutput:
        self.calls += 1
        time.sleep(self._real_sleep_s)
        return FlakyApiOutput(
            resource=request.resource, payload="late-but-valid", source="live"
        )


class HangingTool(Tool[FlakyApiInput, FlakyApiOutput]):
    """Reports no latency at all and then blocks, like a dead socket."""

    name = "hung_api"
    input_model = FlakyApiInput
    output_model = FlakyApiOutput

    def __init__(self, *, block_s: float = 5.0) -> None:
        self._block_s = block_s
        self._never_set = threading.Event()

    def next_latency_ms(self, request: FlakyApiInput, attempt: int = 0) -> float:
        return 0.0

    def run(self, request: FlakyApiInput, attempt: int = 0) -> FlakyApiOutput:
        self._never_set.wait(self._block_s)
        return FlakyApiOutput(
            resource=request.resource, payload="eventually", source="live"
        )


def _orch(tool, clock, **kw):
    return Orchestrator(
        [tool],
        clock=clock,
        policy=RetryPolicy(max_attempts=1),
        default_timeout_ms=500.0,
        **kw,
    )


def _classify_events(orch):
    return [e for e in orch.last_events if e.event == "classify"]


def _result_events(orch):
    return [e for e in orch.last_events if e.event == "tool_result"]


# --- defect 1: the liar must be cut off ----------------------------------- #


def test_tool_that_underreports_its_latency_is_cut_off(clock):
    tool = LyingLatencyTool(declared_ms=10.0, real_sleep_s=1.5)
    orch = _orch(tool, clock)
    plan = Plan(
        goal="g",
        steps=[ToolStep(tool="slow_api", args={"resource": "r"}, timeout_ms=500)],
    )

    started = time.monotonic()
    result = orch.run(plan, run_id="liar")
    wall_ms = (time.monotonic() - started) * 1000.0

    # It claimed 10ms and slept 1500ms. The deadline was 500ms.
    assert result.status == "failed"
    assert FailureClass.TIMEOUT.value in result.failure_classes
    # Cut off at the budget, not after the tool finished its nap.
    assert wall_ms < 1200.0, f"run took {wall_ms:.0f}ms; the watchdog did not fire"
    # And the trace records what actually happened, not the declared 10ms.
    classified = _classify_events(orch)
    assert classified, "no classify event emitted"
    measured = classified[-1].latency_ms
    assert measured is not None, "the timeout was recorded with no measured duration"
    assert measured >= 400.0, f"recorded {measured}ms; expected the real ~500ms"


def test_tool_that_hangs_and_reports_nothing_is_cut_off(clock):
    tool = HangingTool(block_s=5.0)
    orch = _orch(tool, clock)
    plan = Plan(
        goal="g",
        steps=[ToolStep(tool="hung_api", args={"resource": "r"}, timeout_ms=300)],
    )

    started = time.monotonic()
    result = orch.run(plan, run_id="hung")
    wall_ms = (time.monotonic() - started) * 1000.0

    assert result.status == "failed"
    assert FailureClass.TIMEOUT.value in result.failure_classes
    assert wall_ms < 2000.0, f"run took {wall_ms:.0f}ms; a hang was not bounded"


def test_successful_call_is_traced_with_its_measured_duration(clock):
    # Declares 5ms, really takes ~80ms, finishes inside the 500ms budget.
    tool = LyingLatencyTool(declared_ms=5.0, real_sleep_s=0.08)
    orch = _orch(tool, clock)
    plan = Plan(
        goal="g",
        steps=[ToolStep(tool="slow_api", args={"resource": "r"}, timeout_ms=500)],
    )

    result = orch.run(plan, run_id="measured")

    assert result.status == "ok"
    results = _result_events(orch)
    assert len(results) == 1
    latency = results[0].latency_ms
    assert latency is not None
    assert latency >= 60.0, f"traced {latency}ms; the tool only claimed 5ms"


def test_declared_latency_is_kept_as_trace_metadata(clock):
    """The self-reported number is still useful; it just decides nothing."""
    tool = LyingLatencyTool(declared_ms=5.0, real_sleep_s=0.08)
    orch = _orch(tool, clock)
    plan = Plan(
        goal="g",
        steps=[ToolStep(tool="slow_api", args={"resource": "r"}, timeout_ms=500)],
    )
    orch.run(plan, run_id="declared")

    detail = _result_events(orch)[0].detail or ""
    args = _result_events(orch)[0].args or {}
    assert "declared" in detail.lower() or "declared_ms" in args
