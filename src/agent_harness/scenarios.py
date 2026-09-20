"""Ready-to-run harness setups for each demo scenario.

A scenario bundles the tool wiring (including each flaky tool's per-attempt
script and cache), the safety-gate policy, and the retry policy, then returns a
configured :class:`~agent_harness.orchestrator.Orchestrator` plus its plan. Both
the CLI demo and the tests build harnesses this way, so what you see in the
terminal is exactly what the tests assert.
"""

from __future__ import annotations

from pathlib import Path

from .clock import Clock
from .limits import RunLimits
from .orchestrator import Orchestrator
from .planner import ScriptedPlanner
from .retry import RetryPolicy
from .safety import SafetyGate
from .schemas import Plan
from .tools.calculator import CalculatorTool
from .tools.doc_search import DocSearchTool
from .tools.flaky_api import FlakyApiTool

SCENARIOS = (
    "flaky_recovery",
    "timeout_then_cache",
    "real_hang_recovery",
    "unsafe_blocked",
)


def build_harness(
    scenario: str,
    clock: Clock,
    *,
    trace_dir: Path | str | None = None,
) -> tuple[Orchestrator, Plan]:
    """Construct the orchestrator + plan for a named scenario."""
    planner = ScriptedPlanner.from_fixtures()
    plan = planner.plan(scenario)
    limits = RunLimits()

    if scenario == "flaky_recovery":
        flaky = FlakyApiTool(
            script=["transient", "transient", "ok"],
            cache={"order-8841": "cached-order-8841"},
        )
    elif scenario == "timeout_then_cache":
        # Always hangs -> every attempt times out -> must degrade to cache.
        flaky = FlakyApiTool(
            script=["timeout"],
            cache={"order-8841": "stale-order-8841"},
        )
    elif scenario == "real_hang_recovery":
        # Attempt 0 really blocks (only the watchdog ends it), attempt 1 fails
        # transiently, attempt 2 answers. The script restarts per step, so the
        # second flaky step hangs again and its retries run the ledger out.
        flaky = FlakyApiTool(
            script=["hang", "transient", "ok"],
            cache={"order-8841": "stale-order-8841"},
        )
        # A cent, which three attempts at $0.002 plus a search do not quite
        # reach and five attempts pass.
        limits = RunLimits(max_cost_usd=0.01)
    elif scenario == "unsafe_blocked":
        flaky = FlakyApiTool(script=["ok"])
    else:  # pragma: no cover - guarded by caller
        raise KeyError(f"unknown scenario '{scenario}'")

    tools = [DocSearchTool(), CalculatorTool(), flaky]
    orch = Orchestrator(
        tools,
        clock=clock,
        safety=SafetyGate(),                 # nothing allow-listed: deletes blocked
        policy=RetryPolicy(max_attempts=3, base_ms=50.0, factor=2.0),
        limits=limits,
        trace_dir=trace_dir,
        default_timeout_ms=500.0,
    )
    return orch, plan
