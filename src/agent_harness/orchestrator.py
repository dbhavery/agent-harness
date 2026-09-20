"""Deterministic orchestrator: plan -> (safety-gate -> execute -> observe) -> respond.

The loop is intentionally small and inspectable. For each planned step it:

1. runs the :class:`~agent_harness.safety.SafetyGate` (blocks unsafe actions);
2. executes the tool through a validated executor whose deadline is enforced by
   a wall-clock watchdog (:mod:`agent_harness.watchdog`), so a tool that
   under-reports its latency or hangs outright is cut off;
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
    HALTING,
    CostLimitExceeded,
    FailureClass,
    HarnessError,
    ToolTimeout,
    classify,
)
from .clock import Clock, SystemClock
from .limits import CostLedger, RunLimits
from .retry import AttemptRecord, RetryPolicy, run_with_retry
from .safety import SafetyGate
from .schemas import Plan, RunResult, ToolStep
from .tools.base import Tool
from .tools.flaky_api import FlakyApiTool
from .watchdog import call_with_watchdog


@dataclass(slots=True)
class StepOutcome:
    tool: str
    status: str                       # "ok" | "degraded" | "failed"
    summary: str
    latency_ms: float | None = None
    failure_class: FailureClass | None = None
    #: Set when this step tripped a run-level ceiling, which ends the run.
    halt_reason: str | None = None


@dataclass(slots=True)
class Measured:
    """Timing for one execution attempt.

    ``elapsed_ms`` is what the deadline was checked against and what the trace
    records. ``declared_ms`` is the tool's own claim about itself, kept only as
    metadata: it decides nothing.
    """

    elapsed_ms: float
    declared_ms: float


def _execute_once(
    tool: Tool,
    raw_args: dict[str, Any],
    deadline_ms: float | None,
    clock: Clock,
    attempt: int,
    ledger: CostLedger,
) -> tuple[Any, Measured]:
    """One validated attempt, bounded by a wall-clock watchdog.

    The deadline is enforced against *measured* time, never against the tool's
    self-reported ``next_latency_ms``. A tool that lies about its latency, or
    that hangs and reports nothing, is cut off by
    :func:`~agent_harness.watchdog.call_with_watchdog`.

    The declared latency still has a job: the scripted demo tools model slowness
    by declaring it, and that simulated wait is spent through the injected clock
    so the demo stays deterministic. It is capped at the budget, and what the
    budget is compared against afterwards is elapsed time.

    Raises classified :class:`HarnessError` subclasses on any contract breach or
    fault. Returns ``(validated_output, Measured)`` on success.
    """
    request = tool.validate_input(raw_args)          # -> SchemaViolation

    # Pay before the call. A metered endpoint bills for a request that times out
    # just as it bills for one that answers, and every retry is another request.
    price = tool.cost_of(request, attempt)
    if ledger.would_exceed(price):
        raise CostLimitExceeded(
            f"{tool.name} would take the run past its ${ledger.max_cost_usd:.2f} "
            "cost ceiling",
            detail=(
                f"spent ${ledger.spent_usd:.4f} over {ledger.charges} charge(s); "
                f"next call costs ${price:.4f}; ceiling ${ledger.max_cost_usd:.2f}"
            ),
        )
    ledger.charge(tool.name, price)

    declared_ms = tool.next_latency_ms(request, attempt)

    started = clock.now()
    simulated_ms = declared_ms if deadline_ms is None else min(declared_ms, deadline_ms)
    if simulated_ms > 0:
        clock.sleep(simulated_ms / 1000.0)
    elapsed_ms = (clock.now() - started) * 1000.0

    remaining_ms: float | None = None
    if deadline_ms is not None:
        remaining_ms = deadline_ms - elapsed_ms
        if remaining_ms <= 0:
            raise ToolTimeout(
                f"{tool.name} exceeded its {deadline_ms:.0f}ms deadline",
                detail=(
                    f"measured {elapsed_ms:.0f}ms >= budget {deadline_ms:.0f}ms "
                    f"(tool declared {declared_ms:.0f}ms)"
                ),
                measured_ms=elapsed_ms,
            )

    outcome = call_with_watchdog(
        lambda: tool.run(request, attempt),          # -> Transient / MissingContext
        deadline_ms=remaining_ms,
        label=tool.name,
        clock=clock,
        already_elapsed_ms=elapsed_ms,
    )
    out = tool.validate_output(outcome.value)        # -> MalformedOutput
    return out, Measured(elapsed_ms=outcome.elapsed_ms, declared_ms=declared_ms)


class Orchestrator:
    def __init__(
        self,
        tools: dict[str, Tool] | list[Tool],
        *,
        clock: Clock | None = None,
        safety: SafetyGate | None = None,
        policy: RetryPolicy | None = None,
        limits: RunLimits | None = None,
        trace_dir: Path | str | None = None,
        default_timeout_ms: float = 500.0,
    ) -> None:
        if isinstance(tools, list):
            tools = {t.name: t for t in tools}
        self.tools = tools
        self.clock = clock or SystemClock()
        self.safety = safety or SafetyGate()
        self.policy = policy or RetryPolicy()
        # Ceilings are on by default: a spend limit nobody remembers to set is
        # not a spend limit.
        self.limits = limits or RunLimits()
        self.trace_dir = trace_dir
        self.default_timeout_ms = default_timeout_ms

    # ------------------------------------------------------------------ #
    def run(self, plan: Plan, *, run_id: str | None = None) -> RunResult:
        run_id = run_id or uuid.uuid4().hex[:12]
        bus = tr.TraceBus(run_id, self.clock, trace_dir=self.trace_dir)
        step_id = 0
        outcomes: list[StepOutcome] = []
        failure_classes: list[str] = []
        ledger = CostLedger(max_cost_usd=self.limits.max_cost_usd)
        halt_reason: str | None = None

        bus.emit(
            tr.PLAN,
            step_id=step_id,
            detail=f"goal={plan.goal!r}; {len(plan.steps)} step(s)",
            args={
                "steps": [s.tool for s in plan.steps],
                "max_cost_usd": self.limits.max_cost_usd,
                "max_steps": self.limits.max_steps,
            },
        )

        for step in plan.steps:
            # Step ceiling, checked before the step and not after it, so the
            # step past the ceiling is never executed.
            if (
                self.limits.max_steps is not None
                and len(outcomes) >= self.limits.max_steps
            ):
                halt_reason = self._emit_halt(
                    bus,
                    step_id + 1,
                    None,
                    FailureClass.STEP_LIMIT,
                    f"executed {len(outcomes)} of {len(plan.steps)} planned step(s); "
                    f"ceiling is {self.limits.max_steps}",
                )
                failure_classes.append(FailureClass.STEP_LIMIT.value)
                break

            step_id += 1
            outcome = self._run_step(bus, step_id, step, ledger)
            outcomes.append(outcome)
            if outcome.failure_class is not None:
                failure_classes.append(outcome.failure_class.value)
            if outcome.halt_reason is not None:
                halt_reason = outcome.halt_reason
                break

        skipped = len(plan.steps) - len(outcomes)
        result = self._finalize(
            run_id,
            plan,
            outcomes,
            failure_classes,
            bus,
            halt_reason=halt_reason,
            steps_skipped=skipped,
            cost_usd=ledger.spent_usd,
        )
        bus.emit(
            tr.FINAL,
            step_id=step_id + 1,
            outcome=result.status,
            detail=result.answer,
            args={
                "steps_ok": result.steps_ok,
                "steps_degraded": result.steps_degraded,
                "steps_failed": result.steps_failed,
                "steps_skipped": result.steps_skipped,
                "cost_usd": result.cost_usd,
            },
        )
        bus.close()
        # Expose the trace for post-run introspection (tests, tooling).
        self.last_events = list(bus.events)
        self.last_result = result
        return result

    # ------------------------------------------------------------------ #
    def _run_step(
        self,
        bus: tr.TraceBus,
        step_id: int,
        step: ToolStep,
        ledger: CostLedger,
    ) -> StepOutcome:
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
            (output, measured), _records = run_with_retry(
                lambda attempt: _execute_once(
                    tool, step.args, deadline, self.clock, attempt, ledger
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
                # Measured duration of the failed attempt, when the watchdog or
                # the deadline check timed it. Never the tool's own claim.
                latency_ms=getattr(exc, "measured_ms", None),
                classification=fc.value,
                detail=f"retries exhausted / non-retryable: {detail}",
            )
            if fc in HALTING:
                return self._halt(bus, step_id, tool.name, fc, detail)
            return self._fallback(bus, step_id, tool, step, fc)

        # 3) Success.
        summary = _summarize(tool.name, output)
        bus.emit(
            tr.TOOL_RESULT,
            step_id=step_id,
            tool=tool.name,
            outcome="ok",
            latency_ms=measured.elapsed_ms,
            args={"declared_ms": measured.declared_ms},
            detail=f"{summary} [measured {measured.elapsed_ms:.0f}ms, "
            f"declared {measured.declared_ms:.0f}ms]",
        )
        return StepOutcome(
            tool=tool.name,
            status="ok",
            summary=summary,
            latency_ms=measured.elapsed_ms,
        )

    # ------------------------------------------------------------------ #
    def _emit_halt(
        self,
        bus: tr.TraceBus,
        step_id: int,
        tool_name: str | None,
        fc: FailureClass,
        detail: str,
    ) -> str:
        """Record a ceiling breach on the trace and return the halt reason."""
        bus.emit(
            tr.LIMIT,
            step_id=step_id,
            tool=tool_name,
            outcome="halted",
            classification=fc.value,
            detail=f"run halted: {detail}",
        )
        return f"{fc.value}: {detail}"

    def _halt(
        self,
        bus: tr.TraceBus,
        step_id: int,
        tool_name: str | None,
        fc: FailureClass,
        detail: str,
    ) -> StepOutcome:
        """Stop the run on a ceiling breach, with no fallback path.

        A ceiling is not a per-step fault. Serving a cached answer here and then
        carrying on to the next paid step is exactly what the ceiling exists to
        prevent, so this path deliberately has no degrade.
        """
        reason = self._emit_halt(bus, step_id, tool_name, fc, detail)
        return StepOutcome(
            tool=tool_name or "(run)",
            status="failed",
            summary=f"run halted at {fc.value}: {detail}",
            failure_class=fc,
            halt_reason=reason,
        )

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
        *,
        halt_reason: str | None = None,
        steps_skipped: int = 0,
        cost_usd: float = 0.0,
    ) -> RunResult:
        ok = sum(1 for o in outcomes if o.status == "ok")
        degraded = sum(1 for o in outcomes if o.status == "degraded")
        failed = sum(1 for o in outcomes if o.status == "failed")

        if failed and not (ok or degraded):
            status = "failed"
        elif failed or degraded:
            status = "degraded"
        elif halt_reason is not None:
            # Every attempted step worked, but the run stopped short of its plan.
            # Calling that "ok" would hide the ceiling that fired.
            status = "degraded"
        else:
            status = "ok"

        answer_bits = [o.summary for o in outcomes]
        answer = " | ".join(answer_bits) if answer_bits else "(no steps)"
        if halt_reason is not None and steps_skipped:
            answer += f" | {steps_skipped} planned step(s) not attempted"

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
            halted=halt_reason is not None,
            halt_reason=halt_reason,
            steps_skipped=steps_skipped,
            cost_usd=cost_usd,
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
