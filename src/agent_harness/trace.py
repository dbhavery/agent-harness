"""Structured trace bus.

Every meaningful step in a run — plan, tool call, tool result, retry, safety
block, classification, fallback, final answer — is emitted as one typed
:class:`TraceEvent` and appended to a JSONL file under ``traces/``. The report
renderer and the tests both read these records, so the trace is the single
source of truth for "what actually happened".

Timestamps come from an injected :class:`~agent_harness.clock.Clock`, so a run
driven by :class:`~agent_harness.clock.FakeClock` produces byte-stable traces.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .clock import Clock


class EventType(str):
    """Namespace of the event kinds we emit (plain strings for JSON-friendliness)."""


# Event kind constants.
PLAN = "plan"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
RETRY = "retry"
SAFETY_BLOCK = "safety_block"
CLASSIFY = "classify"
FALLBACK = "fallback"
LIMIT = "limit"        # a run-level ceiling (cost, steps) stopped the run
FINAL = "final"


@dataclass(slots=True)
class TraceEvent:
    """One structured record on the trace bus."""

    run_id: str
    step_id: int
    ts: float
    event: str
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    outcome: str | None = None            # "ok" | "error" | "blocked" | None
    latency_ms: float | None = None
    classification: str | None = None     # a FailureClass value, when relevant
    attempt: int | None = None
    detail: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)


class TraceBus:
    """Collects events in memory and (optionally) streams them to a JSONL file."""

    def __init__(
        self,
        run_id: str,
        clock: Clock,
        *,
        trace_dir: Path | str | None = None,
    ) -> None:
        self.run_id = run_id
        self._clock = clock
        self.events: list[TraceEvent] = []
        self._path: Path | None = None
        self._fh = None
        if trace_dir is not None:
            trace_dir = Path(trace_dir)
            trace_dir.mkdir(parents=True, exist_ok=True)
            self._path = trace_dir / f"{run_id}.jsonl"
            # Truncate any stale file for this run id so re-runs are clean.
            self._fh = self._path.open("w", encoding="utf-8")

    @property
    def path(self) -> Path | None:
        return self._path

    def emit(
        self,
        event: str,
        *,
        step_id: int,
        tool: str | None = None,
        args: dict[str, Any] | None = None,
        outcome: str | None = None,
        latency_ms: float | None = None,
        classification: str | None = None,
        attempt: int | None = None,
        detail: str | None = None,
    ) -> TraceEvent:
        evt = TraceEvent(
            run_id=self.run_id,
            step_id=step_id,
            ts=round(self._clock.now(), 6),
            event=event,
            tool=tool,
            args=args or {},
            outcome=outcome,
            latency_ms=latency_ms,
            classification=classification,
            attempt=attempt,
            detail=detail,
        )
        self.events.append(evt)
        if self._fh is not None:
            self._fh.write(evt.to_json() + "\n")
            self._fh.flush()
        return evt

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "TraceBus":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def load_events(path: Path | str) -> list[TraceEvent]:
    """Read a JSONL trace file back into :class:`TraceEvent` records."""
    events: list[TraceEvent] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(TraceEvent(**json.loads(line)))
    return events
