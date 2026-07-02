"""Scenario / planner integration tests mirroring the shipped demo."""

from __future__ import annotations

from agent_harness.classification import FailureClass
from agent_harness.planner import ScriptedPlanner
from agent_harness.scenarios import SCENARIOS, build_harness


def test_all_fixture_scenarios_have_plans():
    planner = ScriptedPlanner.from_fixtures()
    for name in SCENARIOS:
        plan = planner.plan(name)
        assert plan.steps


def test_flaky_recovery_scenario_recovers(clock):
    orch, plan = build_harness("flaky_recovery", clock)
    r = orch.run(plan, run_id="s1")
    assert r.status == "ok"
    assert r.steps_ok == 3


def test_timeout_scenario_degrades_to_cache(clock):
    orch, plan = build_harness("timeout_then_cache", clock)
    r = orch.run(plan, run_id="s2")
    assert r.status == "degraded"
    assert FailureClass.TIMEOUT.value in r.failure_classes
    assert "cache" in r.answer


def test_unsafe_scenario_is_blocked(clock):
    orch, plan = build_harness("unsafe_blocked", clock)
    r = orch.run(plan, run_id="s3")
    assert r.status == "failed"
    assert FailureClass.UNSAFE_ACTION.value in r.failure_classes


def test_unknown_scenario_raises(clock):
    import pytest

    with pytest.raises(KeyError):
        build_harness("does_not_exist", clock)
