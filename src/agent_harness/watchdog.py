"""Wall-clock watchdog around a single tool call.

A per-step deadline is only real if something other than the tool enforces it.
Before 2026-09-19 this harness compared the tool's own declared latency against
the budget, which meant a tool that under-reported its latency, or that hung and
reported nothing, ran as long as it liked: a call declaring 10ms and then
sleeping 1.5s came back ``ok`` against a 500ms deadline.

:func:`call_with_watchdog` runs the call on a worker thread and stops waiting at
the deadline. The elapsed time is read from :func:`time.monotonic` around the
call, so nothing the tool says about itself takes part in the decision.

Abandoning the thread rather than joining it is deliberate. A hung socket never
returns, so joining it would reintroduce the defect. The worker is a daemon, so
an abandoned call cannot hold the process open, and its eventual result is
dropped: the step has already been classified as a timeout.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .classification import ToolTimeout


@dataclass(slots=True)
class CallOutcome:
    """What a watched call produced, and how long it really took."""

    value: Any
    elapsed_ms: float


def call_with_watchdog(
    fn: Callable[[], Any],
    *,
    deadline_ms: float | None,
    label: str,
    already_elapsed_ms: float = 0.0,
) -> CallOutcome:
    """Run ``fn()`` under a wall-clock deadline.

    ``deadline_ms`` is the time left for this call. ``already_elapsed_ms`` is
    time the step has spent before the call (simulated latency, in this
    harness), and is folded into the reported and raised durations so the trace
    shows the whole step, not just the tail.

    Raises :class:`~agent_harness.classification.ToolTimeout` when the call is
    still running at the deadline. Any exception raised inside ``fn`` propagates
    unchanged, so the existing classification path is untouched.

    Elapsed time is rounded to whole milliseconds. Sub-millisecond jitter is
    noise here, and rounding keeps a run on a ``FakeClock`` byte-stable.
    """
    if deadline_ms is None:
        started = time.monotonic()
        value = fn()
        elapsed = round((time.monotonic() - started) * 1000.0)
        return CallOutcome(value, already_elapsed_ms + elapsed)

    box: dict[str, Any] = {}
    done = threading.Event()

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=_target, name=f"watchdog::{label}", daemon=True)
    started = time.monotonic()
    worker.start()
    finished = done.wait(deadline_ms / 1000.0)
    elapsed_ms = already_elapsed_ms + round((time.monotonic() - started) * 1000.0)

    if not finished:
        raise ToolTimeout(
            f"{label} was cut off at its {deadline_ms:.0f}ms deadline",
            detail=(
                f"measured {elapsed_ms:.0f}ms with the call still running; "
                "watchdog abandoned it"
            ),
            measured_ms=elapsed_ms,
        )
    if "error" in box:
        raise box["error"]
    return CallOutcome(box.get("value"), elapsed_ms)
