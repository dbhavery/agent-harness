"""Injectable clock abstraction.

Every timestamp and every "wait" in the harness flows through a ``Clock``.
Production code uses :class:`SystemClock`; tests use :class:`FakeClock` so that
runs are fully deterministic and never actually sleep. This is what lets the
trace timestamps and timeout classification be asserted exactly in pytest.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """Minimal clock contract used across the harness."""

    def now(self) -> float:
        """Return the current time in seconds (float)."""
        ...

    def sleep(self, seconds: float) -> None:
        """Advance time by ``seconds`` (really sleeps, or virtually advances)."""
        ...


class SystemClock:
    """Real wall-clock. Used by the CLI demo."""

    def __init__(self, start: float | None = None) -> None:
        # ``start`` lets the demo pin a base epoch so the report timeline reads
        # from t=0 without being tied to the real epoch. Real sleeps still apply.
        self._offset = 0.0 if start is None else (start - time.time())

    def now(self) -> float:
        return time.time() + self._offset

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """Deterministic virtual clock.

    ``sleep`` advances an internal counter instead of blocking, and every
    advance is recorded so tests can assert exactly how long the harness
    "waited" (retry backoff, tool latency, etc.).
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot sleep a negative duration")
        self.sleeps.append(seconds)
        self._t += seconds

    def advance(self, seconds: float) -> None:
        """Advance time without recording a logical sleep (test helper)."""
        self._t += seconds
