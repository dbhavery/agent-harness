"""A run must stop after a bounded number of steps.

Audit finding (2026-09-19): a 40-step plan ran all 40 steps with nothing
stopping it. A loop with no step ceiling is the classic runaway agent, and the
ceiling has to be on by default, since a limit you have to remember to set is
not a limit.
"""

from __future__ import annotations

from agent_harness.classification import FailureClass
from agent_harness.limits import DEFAULT_MAX_STEPS, RunLimits
from agent_harness.orchestrator import Orchestrator
from agent_harness.retry import RetryPolicy
from agent_harness.schemas import Plan, ToolStep
from agent_harness.tools.calculator import CalculatorTool


def _plan(n: int) -> Plan:
    return Plan(
        goal="loop forever",
        steps=[
            ToolStep(tool="calc_units", args={"op": "add", "operands": [1, i]})
            for i in range(n)
        ],
    )


def _orch(clock, **kw):
    return Orchestrator(
        [CalculatorTool()],
        clock=clock,
        policy=RetryPolicy(max_attempts=1),
        **kw,
    )


def test_forty_step_plan_halts_at_an_explicit_step_ceiling(clock):
    orch = _orch(clock, limits=RunLimits(max_steps=12))

    result = orch.run(_plan(40), run_id="steps")

    assert result.steps_ok == 12
    assert result.halted is True
    assert result.steps_skipped == 28
    assert FailureClass.STEP_LIMIT.value in result.failure_classes
    assert "step" in (result.halt_reason or "").lower()
    limits = [e for e in orch.last_events if e.event == "limit"]
    assert len(limits) == 1
    assert limits[0].classification == FailureClass.STEP_LIMIT.value


def test_the_step_ceiling_is_on_by_default(clock):
    # No limits argument at all: the default must still stop a runaway plan.
    orch = _orch(clock)

    result = orch.run(_plan(40), run_id="steps-default")

    assert DEFAULT_MAX_STEPS < 40
    assert result.steps_ok == DEFAULT_MAX_STEPS
    assert result.halted is True
    assert FailureClass.STEP_LIMIT.value in result.failure_classes


def test_a_plan_under_the_ceiling_runs_whole(clock):
    orch = _orch(clock, limits=RunLimits(max_steps=12))

    result = orch.run(_plan(3), run_id="steps-under")

    assert result.status == "ok"
    assert result.steps_ok == 3
    assert result.halted is False
    assert result.steps_skipped == 0


def test_step_ceiling_can_be_disabled_explicitly(clock):
    orch = _orch(clock, limits=RunLimits(max_steps=None, max_cost_usd=None))

    result = orch.run(_plan(40), run_id="steps-off")

    assert result.steps_ok == 40
    assert result.halted is False
