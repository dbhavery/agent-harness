"""Bounded retry with exponential backoff, keyed off the failure taxonomy.

The retry loop is deliberately a small, standalone unit so it can be tested in
isolation: given a function that fails a scripted number of times, it retries
only *retryable* classes, sleeps an exponentially growing (capped) backoff via
the injected clock, and gives up after ``max_attempts``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

from .classification import FailureClass, classify, is_retryable
from .clock import Clock

T = TypeVar("T")


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 3      # total attempts, including the first
    base_ms: float = 50.0
    factor: float = 2.0
    max_backoff_ms: float = 2000.0

    def backoff_ms(self, attempt: int) -> float:
        """Backoff *after* the given 0-based attempt index, capped."""
        delay = self.base_ms * (self.factor ** attempt)
        return min(delay, self.max_backoff_ms)

    def should_retry(self, failure_class: FailureClass, attempt: int) -> bool:
        return is_retryable(failure_class) and (attempt + 1) < self.max_attempts


@dataclass(slots=True)
class AttemptRecord:
    attempt: int
    failure_class: FailureClass
    detail: str
    backoff_ms: float | None = None  # backoff slept after this attempt, if any


def run_with_retry(
    fn: Callable[[int], T],
    policy: RetryPolicy,
    clock: Clock,
    *,
    on_retry: Callable[[AttemptRecord], None] | None = None,
) -> tuple[T, list[AttemptRecord]]:
    """Run ``fn(attempt)`` with bounded retries.

    Returns ``(result, attempt_records)`` on eventual success. If every attempt
    fails (or the failure is non-retryable), the last exception is re-raised
    with the accumulated records attached as ``exc.attempt_records``.
    """
    records: list[AttemptRecord] = []
    for attempt in range(policy.max_attempts):
        try:
            result = fn(attempt)
            return result, records
        except BaseException as exc:  # noqa: BLE001 - we classify then re-raise
            fc = classify(exc)
            detail = getattr(exc, "detail", None) or str(exc)
            record = AttemptRecord(attempt=attempt, failure_class=fc, detail=detail)
            if policy.should_retry(fc, attempt):
                delay = policy.backoff_ms(attempt)
                record.backoff_ms = delay
                records.append(record)
                if on_retry is not None:
                    on_retry(record)
                clock.sleep(delay / 1000.0)
                continue
            records.append(record)
            setattr(exc, "attempt_records", records)
            raise
    raise AssertionError("unreachable: retry loop exhausted without return/raise")
