"""Wall-clock watchdog around a single tool call.

A per-step deadline is only real if something other than the tool enforces it.
Before 2026-09-19 this harness compared the tool's own declared latency against
the budget and never timed the call, so a tool that under-reported its latency,
or that hung and reported nothing, ran as long as it liked: a call declaring 10ms
and then sleeping 1.5s came back ``ok`` against a 500ms deadline.

:func:`call_with_watchdog` runs the call on a worker thread and stops waiting at
the deadline. Two clocks are involved, deliberately:

* **Enforcement is always real.** The wait is ``threading.Event.wait`` in real
  seconds, because a hung call does not advance a virtual clock and cannot be
  asked how long it has been stuck. Nothing the tool says about itself takes
  part in the decision.
* **Reporting follows the injected clock**, which is the harness's own notion of
  time: real under :class:`~agent_harness.clock.SystemClock`, virtual under
  :class:`~agent_harness.clock.FakeClock`. That is what keeps a scripted demo
  run byte-stable, and it is why the measured duration of a cut-off call is
  reported as the budget it consumed rather than a jittery sample.

Abandoning the thread rather than joining it is deliberate. A hung socket never
returns, so joining it would reintroduce the defect. The worker is a daemon, so
an abandoned call cannot hold the process open, and its eventual result is
dropped: the step has already been classified as a timeout.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from .classification import ToolTimeout
from .clock import Clock


@dataclass(slots=True)
class CallOutcome:
    """What a watched call produced, and how long it took."""

    value: Any
    elapsed_ms: float


def call_with_watchdog(
    fn: Callable[[], Any],
    *,
    deadline_ms: float | None,
    label: str,
    clock: Clock,
    already_elapsed_ms: float = 0.0,
) -> CallOutcome:
    """Run ``fn()`` under a deadline it does not get a say in.

    ``deadline_ms`` is the time left for this call. ``already_elapsed_ms`` is
    time the step spent before the call, folded into the reported and raised
    durations so the trace shows the whole step rather than just its tail.

    Raises :class:`~agent_harness.classification.ToolTimeout` when the call is
    still running at the deadline, carrying the duration the step consumed. Any
    exception raised inside ``fn`` propagates unchanged, so the existing
    classification path is untouched.
    """
    if deadline_ms is None:
        started = clock.now()
        value = fn()
        return CallOutcome(value, already_elapsed_ms + _ms_since(clock, started))

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
    started = clock.now()
    worker.start()
    # Real seconds: the only kind a hang responds to.
    finished = done.wait(deadline_ms / 1000.0)

    if not finished:
        # The call ran out the whole remaining budget and is still going.
        elapsed_ms = already_elapsed_ms + deadline_ms
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
    return CallOutcome(box.get("value"), already_elapsed_ms + _ms_since(clock, started))


def _ms_since(clock: Clock, started: float) -> float:
    # Rounded to microseconds: enough resolution for a trace, and it keeps the
    # JSONL from carrying float noise.
    return round((clock.now() - started) * 1000.0, 3)
