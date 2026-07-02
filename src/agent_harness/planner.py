"""Planners turn a goal into a :class:`~agent_harness.schemas.Plan`.

The default planner is deterministic and fixture-driven so the harness runs
fully offline with reproducible output. A real-LLM planner is available behind
environment variables only; it is never required for the demo or the tests.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .schemas import Plan, ToolStep

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class ScriptedPlanner:
    """Returns a canned plan for a known goal/scenario. No model, no network."""

    def __init__(self, plans: dict[str, Plan] | None = None) -> None:
        self._plans = plans or {}

    @classmethod
    def from_fixtures(cls, path: Path | str | None = None) -> "ScriptedPlanner":
        path = Path(path) if path else FIXTURES_DIR / "plans.json"
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        plans = {
            name: Plan(
                goal=spec["goal"],
                steps=[ToolStep(**s) for s in spec["steps"]],
            )
            for name, spec in raw.items()
        }
        return cls(plans)

    def scenarios(self) -> list[str]:
        return sorted(self._plans)

    def plan(self, scenario: str) -> Plan:
        if scenario not in self._plans:
            raise KeyError(
                f"unknown scenario '{scenario}'; known: {', '.join(self.scenarios())}"
            )
        return self._plans[scenario]


class LLMPlanner:
    """Optional real-LLM planner. Import-light; only touches the SDK on use.

    Enabled only when ``AGENT_HARNESS_LLM=1`` and an API key are set. This keeps
    the harness honest: the shipped demo and CI never depend on paid calls.
    """

    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.environ.get("AGENT_HARNESS_MODEL", "")

    @staticmethod
    def enabled() -> bool:
        return os.environ.get("AGENT_HARNESS_LLM") == "1" and bool(
            os.environ.get("ANTHROPIC_API_KEY")
        )

    def plan(self, goal: str) -> Plan:  # pragma: no cover - requires paid API
        if not self.enabled():
            raise RuntimeError(
                "LLMPlanner requires AGENT_HARNESS_LLM=1 and ANTHROPIC_API_KEY. "
                "Use ScriptedPlanner for offline runs."
            )
        raise NotImplementedError(
            "Real-LLM planning is intentionally left as an opt-in integration "
            "point. Wire your Anthropic client here; the orchestrator consumes "
            "the same typed Plan either way."
        )
