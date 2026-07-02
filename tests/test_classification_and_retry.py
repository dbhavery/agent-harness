"""Failure-taxonomy and retry/backoff unit tests."""

from __future__ import annotations

import pytest

from agent_harness.classification import (
    FailureClass,
    MalformedOutput,
    SchemaViolation,
    TransientToolError,
    classify,
    is_retryable,
)
from agent_harness.retry import RetryPolicy, run_with_retry


def test_classify_known_errors():
    assert classify(TransientToolError("x")) is FailureClass.TRANSIENT
    assert classify(SchemaViolation("x")) is FailureClass.SCHEMA_VIOLATION
    assert classify(MalformedOutput("x")) is FailureClass.MALFORMED_OUTPUT


def test_classify_unknown_value_error_is_malformed():
    assert classify(ValueError("boom")) is FailureClass.MALFORMED_OUTPUT


def test_classify_unknown_exception_is_transient():
    assert classify(RuntimeError("weird")) is FailureClass.TRANSIENT


def test_retryable_only_transient_and_timeout():
    assert is_retryable(FailureClass.TRANSIENT)
    assert is_retryable(FailureClass.TIMEOUT)
    assert not is_retryable(FailureClass.SCHEMA_VIOLATION)
    assert not is_retryable(FailureClass.UNSAFE_ACTION)


def test_backoff_is_exponential_and_capped():
    p = RetryPolicy(base_ms=50, factor=2.0, max_backoff_ms=150)
    assert p.backoff_ms(0) == 50
    assert p.backoff_ms(1) == 100
    assert p.backoff_ms(2) == 150  # 200 capped to 150
    assert p.backoff_ms(3) == 150


def test_run_with_retry_recovers_after_transient(clock):
    calls = {"n": 0}

    def fn(attempt: int) -> str:
        calls["n"] += 1
        if attempt < 2:
            raise TransientToolError("temporary")
        return "ok"

    result, records = run_with_retry(fn, RetryPolicy(max_attempts=3, base_ms=10), clock)
    assert result == "ok"
    assert calls["n"] == 3
    assert len(records) == 2  # two failed attempts before success
    # Backoff was slept via the injected clock: 10ms then 20ms.
    assert clock.sleeps == pytest.approx([0.01, 0.02])


def test_run_with_retry_gives_up_after_max_attempts(clock):
    def fn(attempt: int) -> str:
        raise TransientToolError("always fails")

    with pytest.raises(TransientToolError) as ei:
        run_with_retry(fn, RetryPolicy(max_attempts=3, base_ms=10), clock)
    # Records are attached to the exception for post-mortem.
    assert len(ei.value.attempt_records) == 3


def test_run_with_retry_does_not_retry_non_retryable(clock):
    calls = {"n": 0}

    def fn(attempt: int) -> str:
        calls["n"] += 1
        raise SchemaViolation("bad input")

    with pytest.raises(SchemaViolation):
        run_with_retry(fn, RetryPolicy(max_attempts=3), clock)
    assert calls["n"] == 1  # schema violations are not retried
    assert clock.sleeps == []
