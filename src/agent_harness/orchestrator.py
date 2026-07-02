"""Deterministic orchestrator: plan -> (safety-gate -> execute -> observe) -> respond.

The loop is intentionally small and inspectable. For each planned step it:

1. runs the :class:`~agent_harness.safety.SafetyGate` (blocks unsafe actions);
2. executes the tool through a validated, deadline-bounded executor;
3. retries retryable failures with exponential backoff;
4. on exhausted retries, takes a fallback path (cached/secondary source) or a
   safe refusal;
5. emits a structured trace event at every decision point.

It never calls ``time.time()`` directly — all timing flows through the injected
clock, so a run under :class:`~agent_harness.clock.FakeClock` is fully
reproducible.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import trace as tr
from .classification import (
    FailureClass,
    HarnessError,
    ToolTimeout,
    classify,
)
from .clock import Clock, SystemClock
from .retry import AttemptRecord, RetryPolicy, run_with_retry
from .safety import SafetyGate
from .schemas import Plan, RunResult, ToolStep
from .tools.base import Tool
from .tools.flaky_api import FlakyApiTool


@dataclass(slots=True)
class StepOutcome:
    tool: str
    status: str                       # "ok" | "degraded" | "failed"
    summary: str
    latency_ms: float | None = None
    failure_class: FailureClass | None = None


def _execute_once(
    tool: Tool,
    raw_args: dict[str, Any],
    deadline_ms: float | None,
    clock: Clock,
    attempt: int,
) -> tuple[Any, float]:
    """One validated, deadline-bounded execution attempt.

    Raises classified :class:`HarnessError` subclasses on any contract breach or
    fault. Returns ``(validated_output, latency_ms)`` on success.
    """
    request = tool.validate_input(raw_args)          # -> SchemaViolation
    latency = tool.next_latency_ms(request, attempt)
    if deadline_ms is not None and latency > deadline_ms:
        # Model a hang: we "wait" up to the budget, then abort as a timeout.
        clock.sleep(deadline_ms / 1000.0)
        raise ToolTimeout(
            f"{tool.name} exceeded {deadline_ms:.0f}ms deadline",
            detail=f"declared latency {latency:.0f}ms > budget {deadline_ms:.0f}ms",
        )
    clock.sleep(latency / 1000.0)
    raw_out = tool.run(request, attempt)             # -> Transient / MissingContext
    out = tool.validate_output(raw_out)              # -> MalformedOutput
    return out, latency


class Orchestrator:
    def __init__(
        self,
        tools: dict[str, Tool] | list[Tool],
        *,
        clock: Clock | None = None,
        safety: SafetyGate | None = None,
        policy: RetryPolicy | None = None,
        trace_dir: Path | str | None = None,
        default_timeout_ms: float = 500.0,
    ) -> None:
        if isinstance(tools, list):
            tools = {t.name: t for t in tools}
        self.tools = tools
        self.clock = clock or SystemClock()
        self.safety = safety or SafetyGate()
        self.policy = policy or RetryPolicy()
        self.trace_dir = trace_dir
        self.default_timeout_ms = default_timeout_ms

    # ------------------------------------------------------------------ #
    def run(self, plan: Plan, *, run_id: str | None = None) -> RunResult:
        run_id = run_id or uuid.uuid4().hex[:12]
        bus = tr.TraceBus(run_id, self.clock, trace_dir=self.trace_dir)
        step_id = 0
        outcomes: list[StepOutcome] = []
        failure_classes: list[str] = []

        bus.emit(
            tr.PLAN,
            step_id=step_id,
            detail=f"goal={plan.goal!r}; {len(plan.steps)} step(s)",
            args={"steps": [s.tool for s in plan.steps]},
        )

        for step in plan.steps:
            step_id += 1
            outcome = self._run_step(bus, step_id, step)
            outcomes.append(outcome)
            if outcome.failure_class is not None:
                failure_classes.append(outcome.failure_class.value)

        result = self._finalize(run_id, plan, outcomes, failure_classes, bus)
        bus.emit(
            tr.FINAL,
            step_id=step_id + 1,
            outcome=result.status,
            detail=result.answer,
            args={
                "steps_ok": result.steps_ok,
                "steps_degraded": result.steps_degraded,
                "steps_failed": result.steps_failed,
            },
        )
        bus.close()
        # Expose the trace for post-run introspection (tests, tooling).
        self.last_events = list(bus.events)
        self.last_result = result
        return result

    # ------------------------------------------------------------------ #
    def _run_step(self, bus: tr.TraceBus, step_id: int, step: ToolStep) -> StepOutcome:
        tool = self.tools.get(step.tool)
        if tool is None:
            bus.emit(
                tr.TOOL_RESULT,
                step_id=step_id,
                tool=step.tool,
                outcome="error",
                classification=FailureClass.MISSING_CONTEXT.value,
                detail=f"no tool registered under name '{step.tool}'",
            )
            return StepOutcome(
                tool=step.tool,
                status="failed",
                summary=f"unknown tool '{step.tool}'",
                failure_class=FailureClass.MISSING_CONTEXT,
            )

        deadline = step.timeout_ms if step.timeout_ms is not None else self.default_timeout_ms

        bus.emit(
            tr.TOOL_CALL,
            step_id=step_id,
            tool=tool.name,
            args=step.args,
            detail=f"deadline={deadline:.0f}ms",
        )

        # 1) Safety gate — before any execution.
        decision = self.safety.check(tool.name, step.args)
        if not decision.allowed:
            bus.emit(
                tr.SAFETY_BLOCK,
                step_id=step_id,
                tool=tool.name,
                args=step.args,
                outcome="blocked",
                classification=FailureClass.UNSAFE_ACTION.value,
                detail=f"{decision.rule}: {decision.reason}",
            )
            return StepOutcome(
                tool=tool.name,
                status="failed",
                summary=f"blocked by safety gate ({decision.reason})",
                failure_class=FailureClass.UNSAFE_ACTION,
            )

        # 2) Execute with bounded retry.
        def on_retry(rec: AttemptRecord) -> None:
            bus.emit(
                tr.RETRY,
                step_id=step_id,
                tool=tool.name,
                outcome="error",
                classification=rec.failure_class.value,
                attempt=rec.attempt,
                latency_ms=rec.backoff_ms,
                detail=f"attempt {rec.attempt} failed ({rec.detail}); "
                f"backing off {rec.backoff_ms:.0f}ms",
            )

        try:
            (output, latency), _records = run_with_retry(
                lambda attempt: _execute_once(
                    tool, step.args, deadline, self.clock, attempt
                ),
                self.policy,
                self.clock,
                on_retry=on_retry,
            )
        except BaseException as exc:  # noqa: BLE001 - classify, then decide fallback
            fc = classify(exc)
            detail = getattr(exc, "detail", None) or str(exc)
            bus.emit(
                tr.CLASSIFY,
                step_id=step_id,
                tool=tool.name,
                outcome="error",
                classification=fc.value,
                detail=f"retries exhausted / non-retryable: {detail}",
            )
            return self._fallback(bus, step_id, tool, step, fc)

        # 3) Success.
        summary = _summarize(tool.name, output)
        bus.emit(
            tr.TOOL_RESULT,
            step_id=step_id,
            tool=tool.name,
            outcome="ok",
            latency_ms=latency,
            detail=summary,
        )
        return StepOutcome(tool=tool.name, status="ok", summary=summary, latency_ms=latency)

    # ------------------------------------------------------------------ #
    def _fallback(
        self,
        bus: tr.TraceBus,
        step_id: int,
        tool: Tool,
        step: ToolStep,
        fc: FailureClass,
    ) -> StepOutcome:
        """Degrade gracefully after a step's primary path failed."""
        # Cached/secondary source for the external API tool.
        if isinstance(tool, FlakyApiTool):
            resource = str(step.args.get("resource", ""))
            cached = tool.cached(resource)
            if cached is not None:
                summary = _summarize(tool.name, cached)
                bus.emit(
                    tr.FALLBACK,
                    step_id=step_id,
                    tool=tool.name,
                    outcome="degraded",
                    classification=fc.value,
                    detail=f"served stale cache for '{resource}' after {fc.value}",
                )
                return StepOutcome(
                    tool=tool.name,
                    status="degraded",
                    summary=summary + " (from cache)",
                    failure_class=fc,
                )

        # No secondary source available -> safe refusal, but keep the run alive.
        bus.emit(
            tr.FALLBACK,
            step_id=step_id,
            tool=tool.name,
            outcome="failed",
            classification=fc.value,
            detail=f"no fallback available for {fc.value}; returning safe refusal",
        )
        return StepOutcome(
            tool=tool.name,
            status="failed",
            summary=f"could not complete '{tool.name}' ({fc.value}); refused safely",
            failure_class=fc,
        )

    # ------------------------------------------------------------------ #
    def _finalize(
        self,
        run_id: str,
        plan: Plan,
        outcomes: list[StepOutcome],
        failure_classes: list[str],
        bus: tr.TraceBus,
    ) -> RunResult:
        ok = sum(1 for o in outcomes if o.status == "ok")
        degraded = sum(1 for o in outcomes if o.status == "degraded")
        failed = sum(1 for o in outcomes if o.status == "failed")

        if failed and not (ok or degraded):
            status = "failed"
        elif failed or degraded:
            status = "degraded"
        else:
            status = "ok"

        answer_bits = [o.summary for o in outcomes]
        answer = " | ".join(answer_bits) if answer_bits else "(no steps)"

        return RunResult(
            run_id=run_id,
            goal=plan.goal,
            status=status,
            answer=answer,
            steps_ok=ok,
            steps_degraded=degraded,
            steps_failed=failed,
            failure_classes=sorted(set(failure_classes)),
            trace_path=str(bus.path) if bus.path else None,
        )


def _summarize(tool_name: str, output: Any) -> str:
    """Human-readable one-liner per tool output, for the final answer."""
    data = output.model_dump() if hasattr(output, "model_dump") else output
    if tool_name == "doc_search":
        hits = data.get("results", [])
        if not hits:
            return "doc_search: no matches"
        top = hits[0]
        return f"doc_search: top hit '{top['title']}' (score {top['score']})"
    if tool_name == "calc_units":
        unit = f" {data['unit']}" if data.get("unit") else ""
        return f"calc_units: = {data['value']}{unit}"
    if tool_name == "flaky_api":
        return f"flaky_api: {data['resource']} -> {data['payload']} [{data['source']}]"
    return f"{tool_name}: {data}"
