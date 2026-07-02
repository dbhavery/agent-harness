"""End-to-end orchestrator resilience tests — one per operational edge case."""

from __future__ import annotations

from agent_harness.classification import FailureClass
from agent_harness.orchestrator import Orchestrator
from agent_harness.retry import RetryPolicy
from agent_harness.safety import SafetyGate
from agent_harness.schemas import Plan, ToolStep
from agent_harness.tools.calculator import CalculatorTool
from agent_harness.tools.doc_search import DocSearchTool
from agent_harness.tools.flaky_api import FlakyApiTool


def _orch(tools, clock, **kw):
    return Orchestrator(
        tools,
        clock=clock,
        policy=RetryPolicy(max_attempts=3, base_ms=10),
        default_timeout_ms=500.0,
        **kw,
    )


def _classes(events):
    return [e.classification for e in events if e.classification]


# --- happy path ----------------------------------------------------------- #


def test_full_plan_all_ok(clock):
    orch = _orch(
        [DocSearchTool(), CalculatorTool(), FlakyApiTool(script=["ok"])], clock
    )
    plan = Plan(
        goal="g",
        steps=[
            ToolStep(tool="doc_search", args={"query": "timeout"}),
            ToolStep(tool="calc_units", args={"op": "add", "operands": [1, 2]}),
            ToolStep(tool="flaky_api", args={"resource": "r1"}),
        ],
    )
    r = orch.run(plan, run_id="ok")
    assert r.status == "ok"
    assert r.steps_ok == 3 and r.steps_failed == 0


# --- edge case: transient failure then recovery --------------------------- #


def test_transient_failures_then_recover(clock):
    flaky = FlakyApiTool(script=["transient", "transient", "ok"])
    orch = _orch([flaky], clock)
    plan = Plan(goal="g", steps=[ToolStep(tool="flaky_api", args={"resource": "r"})])
    r = orch.run(plan, run_id="recover")
    assert r.status == "ok"
    assert r.steps_ok == 1
    # Two retry events with transient classification are present in the trace.
    retries = [e for e in orch_last_events(orch) if e.event == "retry"]
    assert len(retries) == 2
    assert all(e.classification == FailureClass.TRANSIENT.value for e in retries)


# --- edge case: timeout bounded + fallback to cache ----------------------- #


def test_timeout_is_bounded_and_degrades_to_cache(clock):
    flaky = FlakyApiTool(script=["timeout"], cache={"r": "cached-r"})
    orch = _orch([flaky], clock)
    plan = Plan(
        goal="g",
        steps=[ToolStep(tool="flaky_api", args={"resource": "r"}, timeout_ms=200)],
    )
    r = orch.run(plan, run_id="timeout")
    assert r.status == "degraded"
    assert r.steps_degraded == 1
    assert FailureClass.TIMEOUT.value in r.failure_classes
    assert "cache" in r.answer
    # The clock never advanced more than the bounded budget per attempt.
    assert max(clock.sleeps) <= 0.2 + 1e-9


def test_timeout_without_cache_is_safe_refusal(clock):
    flaky = FlakyApiTool(script=["timeout"], cache={})
    orch = _orch([flaky], clock)
    plan = Plan(
        goal="g",
        steps=[ToolStep(tool="flaky_api", args={"resource": "r"}, timeout_ms=100)],
    )
    r = orch.run(plan, run_id="timeout-norecover")
    assert r.status == "failed"
    assert "refused safely" in r.answer
    assert FailureClass.TIMEOUT.value in r.failure_classes


# --- edge case: unsafe action blocked ------------------------------------- #


def test_unsafe_action_blocked_by_gate(clock):
    orch = _orch([FlakyApiTool(script=["ok"])], clock, safety=SafetyGate())
    plan = Plan(
        goal="g",
        steps=[ToolStep(tool="flaky_api", args={"resource": "prod", "action": "delete"})],
    )
    r = orch.run(plan, run_id="unsafe")
    assert r.status == "failed"
    assert FailureClass.UNSAFE_ACTION.value in r.failure_classes
    blocks = [e for e in orch_last_events(orch) if e.event == "safety_block"]
    assert len(blocks) == 1
    # A blocked action is never executed (no tool_result for it).
    assert not any(e.event == "tool_result" for e in orch_last_events(orch))


# --- edge case: malformed tool output ------------------------------------- #


def test_malformed_output_classified_and_not_infinite(clock):
    flaky = FlakyApiTool(script=["malformed"])  # always malformed
    orch = _orch([flaky], clock)
    plan = Plan(goal="g", steps=[ToolStep(tool="flaky_api", args={"resource": "r"})])
    r = orch.run(plan, run_id="malformed")
    assert r.status == "failed"
    assert FailureClass.MALFORMED_OUTPUT.value in r.failure_classes
    # Malformed output is NOT retryable, so only one execution attempt happened.
    assert not any(e.event == "retry" for e in orch_last_events(orch))


# --- edge case: missing context ------------------------------------------- #


def test_missing_context_handled_not_crashed(clock):
    orch = _orch([DocSearchTool(corpus=[])], clock)
    plan = Plan(goal="g", steps=[ToolStep(tool="doc_search", args={"query": "x"})])
    r = orch.run(plan, run_id="missing")
    assert r.status == "failed"
    assert FailureClass.MISSING_CONTEXT.value in r.failure_classes


# --- edge case: schema violation at input boundary ------------------------ #


def test_schema_violation_at_input(clock):
    orch = _orch([CalculatorTool()], clock)
    # top-level op is invalid -> pydantic rejects at the boundary.
    plan = Plan(goal="g", steps=[ToolStep(tool="calc_units", args={"op": "nope"})])
    r = orch.run(plan, run_id="schema")
    assert r.status == "failed"
    assert FailureClass.SCHEMA_VIOLATION.value in r.failure_classes


# --- edge case: unknown tool --------------------------------------------- #


def test_unknown_tool_is_handled(clock):
    orch = _orch([CalculatorTool()], clock)
    plan = Plan(goal="g", steps=[ToolStep(tool="ghost", args={})])
    r = orch.run(plan, run_id="ghost")
    assert r.status == "failed"
    assert FailureClass.MISSING_CONTEXT.value in r.failure_classes


# --- mixed plan: partial degrade still returns a result ------------------- #


def test_mixed_plan_returns_degraded_result(clock):
    flaky = FlakyApiTool(script=["timeout"], cache={"r": "cached-r"})
    orch = _orch([DocSearchTool(), flaky], clock)
    plan = Plan(
        goal="g",
        steps=[
            ToolStep(tool="doc_search", args={"query": "timeout"}),
            ToolStep(tool="flaky_api", args={"resource": "r"}, timeout_ms=200),
        ],
    )
    r = orch.run(plan, run_id="mixed")
    assert r.status == "degraded"
    assert r.steps_ok == 1 and r.steps_degraded == 1
    # A usable final answer is still produced.
    assert "doc_search" in r.answer and "cache" in r.answer


# --- helper --------------------------------------------------------------- #


def orch_last_events(orch: Orchestrator):
    """Events from the orchestrator's most recent run (introspection API)."""
    return orch.last_events
