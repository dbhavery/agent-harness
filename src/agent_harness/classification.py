"""Failure taxonomy and the errors that map onto it.

The whole point of the harness is that when a tool call goes wrong, the failure
is *classified* into a small, fixed set of operational categories rather than
bubbling up as an opaque stack trace. Retry / fallback decisions are keyed off
these classes, so the taxonomy is real control-flow, not just a log label.
"""

from __future__ import annotations

from enum import Enum


class FailureClass(str, Enum):
    """The six operational failure categories the harness distinguishes."""

    MALFORMED_OUTPUT = "malformed_output"   # tool returned unparseable / off-contract data
    MISSING_CONTEXT = "missing_context"     # tool invoked without the data it needs
    TIMEOUT = "timeout"                     # tool exceeded its deadline
    UNSAFE_ACTION = "unsafe_action"         # safety gate blocked the call
    TRANSIENT = "transient"                 # temporary fault, worth retrying
    SCHEMA_VIOLATION = "schema_violation"   # input failed contract validation


# Which classes are worth retrying. Retrying a schema violation or an unsafe
# action is pointless (the input is wrong / disallowed and will stay wrong);
# transient faults and timeouts can succeed on a later attempt.
RETRYABLE: frozenset[FailureClass] = frozenset(
    {FailureClass.TRANSIENT, FailureClass.TIMEOUT}
)


class HarnessError(Exception):
    """Base class for every failure the orchestrator knows how to classify."""

    failure_class: FailureClass

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail or message


class SchemaViolation(HarnessError):
    failure_class = FailureClass.SCHEMA_VIOLATION


class MalformedOutput(HarnessError):
    failure_class = FailureClass.MALFORMED_OUTPUT


class MissingContext(HarnessError):
    failure_class = FailureClass.MISSING_CONTEXT


class ToolTimeout(HarnessError):
    failure_class = FailureClass.TIMEOUT


class UnsafeAction(HarnessError):
    failure_class = FailureClass.UNSAFE_ACTION


class TransientToolError(HarnessError):
    failure_class = FailureClass.TRANSIENT


def classify(exc: BaseException) -> FailureClass:
    """Map an exception onto a :class:`FailureClass`.

    Known harness errors carry their class explicitly. Anything else is treated
    conservatively: a JSON / value / type error looks like malformed output,
    and a truly unknown exception is surfaced as ``transient`` so the harness
    gets one bounded retry rather than crashing the whole run.
    """
    if isinstance(exc, HarnessError):
        return exc.failure_class
    if isinstance(exc, (ValueError, TypeError)):
        return FailureClass.MALFORMED_OUTPUT
    return FailureClass.TRANSIENT


def is_retryable(fc: FailureClass) -> bool:
    return fc in RETRYABLE
