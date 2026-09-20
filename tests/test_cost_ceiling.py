"""A run must stop spending when it reaches its cost ceiling.

Audit finding (2026-09-19): grepping the source for cost|budget|token|ceiling
returned five hits, all of them the word "budget" inside a timeout message. The
harness had no spend limit of any kind, so a retry storm on a metered tool could
bill without bound.

These tests pin the ceiling: a run charges every attempt it makes, including
retries, and halts the moment the next charge would take it over the limit.
"""

from __future__ import annotations

from agent_harness.classification import FailureClass, TransientToolError
from agent_harness.limits import RunLimits
from agent_harness.orchestrator import Orchestrator
from agent_harness.retry import RetryPolicy
from agent_harness.schemas import FlakyApiInput, FlakyApiOutput, Plan, ToolStep
from agent_harness.tools.base import Tool


class MeteredTool(Tool[FlakyApiInput, FlakyApiOutput]):
    """A tool that bills per call, like every real model or API endpoint."""

    name = "metered_api"
    input_model = FlakyApiInput
    output_model = FlakyApiOutput
    latency_ms = 1.0

    def __init__(self, *, cost_usd: float, always_transient: bool = False) -> None:
        self.cost_usd = cost_usd
        self._always_transient = always_transient
        self.calls = 0

    def run(self, request: FlakyApiInput, attempt: int = 0) -> FlakyApiOutput:
        self.calls += 1
        if self._always_transient:
            raise TransientToolError(
                "metered_api upstream 503", detail="temporary upstream failure"
            )
        return FlakyApiOutput(
            resource=request.resource, payload="paid-for", source="live"
        )


def _plan(tool_name: str, n: int) -> Plan:
    return Plan(
        goal="spend money",
        steps=[
            ToolStep(tool=tool_name, args={"resource": f"r{i}"}, timeout_ms=500)
            for i in range(n)
        ],
    )


def _limit_events(orch):
    return [e for e in orch.last_events if e.event == "limit"]


# --- defect 2: the ceiling must halt the run ------------------------------- #


def test_run_halts_when_the_cost_ceiling_is_reached(clock):
    tool = MeteredTool(cost_usd=0.25)
    orch = Orchestrator(
        [tool],
        clock=clock,
        policy=RetryPolicy(max_attempts=1),
        limits=RunLimits(max_cost_usd=1.00),
    )

    result = orch.run(_plan("metered_api", 10), run_id="cost")

    # Four calls at 25 cents fill the dollar; the fifth is never made.
    assert tool.calls == 4
    assert result.cost_usd == 0.25 * 4
    assert result.halted is True
    assert FailureClass.COST_LIMIT.value in result.failure_classes
    assert "cost" in (result.halt_reason or "").lower()
    assert result.steps_skipped == 5
    # The halt is on the record, not just in the return value.
    limits = _limit_events(orch)
    assert len(limits) == 1
    assert limits[0].classification == FailureClass.COST_LIMIT.value


def test_retry_attempts_are_charged_against_the_ceiling(clock):
    # Retries are how an agent harness quietly spends money, so they must bill.
    tool = MeteredTool(cost_usd=0.10, always_transient=True)
    orch = Orchestrator(
        [tool],
        clock=clock,
        policy=RetryPolicy(max_attempts=5, base_ms=1.0),
        limits=RunLimits(max_cost_usd=0.25),
    )

    result = orch.run(_plan("metered_api", 1), run_id="cost-retry")

    assert tool.calls == 2               # 0.20 spent; a third call would pass 0.25
    assert result.cost_usd == 0.20
    assert result.halted is True
    assert FailureClass.COST_LIMIT.value in result.failure_classes


def test_cost_limit_is_never_retried(clock):
    tool = MeteredTool(cost_usd=0.10, always_transient=True)
    orch = Orchestrator(
        [tool],
        clock=clock,
        policy=RetryPolicy(max_attempts=5, base_ms=1.0),
        limits=RunLimits(max_cost_usd=0.25),
    )
    orch.run(_plan("metered_api", 1), run_id="cost-noretry")

    retries = [e for e in orch.last_events if e.event == "retry"]
    # Two transient retries, then the ceiling ends it. Retrying a spend limit
    # would only hit the same wall again.
    assert len(retries) == 2
    assert all(e.classification == FailureClass.TRANSIENT.value for e in retries)


def test_spend_is_reported_on_a_run_that_stays_under_the_ceiling(clock):
    tool = MeteredTool(cost_usd=0.01)
    orch = Orchestrator(
        [tool],
        clock=clock,
        policy=RetryPolicy(max_attempts=1),
        limits=RunLimits(max_cost_usd=1.00),
    )

    result = orch.run(_plan("metered_api", 3), run_id="cost-under")

    assert result.status == "ok"
    assert result.halted is False
    assert result.cost_usd == 0.03
    assert result.steps_skipped == 0
    assert not _limit_events(orch)


def test_a_free_tool_still_runs_under_a_ceiling(clock):
    # The control: a ceiling that fires on a zero-cost tool would be a bug.
    tool = MeteredTool(cost_usd=0.0)
    orch = Orchestrator(
        [tool],
        clock=clock,
        policy=RetryPolicy(max_attempts=1),
        limits=RunLimits(max_cost_usd=0.01),
    )

    result = orch.run(_plan("metered_api", 15), run_id="cost-free")

    assert result.halted is False
    assert result.steps_ok == 15
    assert result.cost_usd == 0.0
