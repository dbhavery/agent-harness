"""A simulated flaky external API.

This is the tool that exercises the reliability machinery. Its behaviour is
fully scripted per-attempt so the demo and the tests are deterministic: given a
script like ``["transient", "transient", "ok"]`` the first two attempts raise a
transient error and the third succeeds — proving retry-then-recover.

Supported per-attempt behaviours:
    * ``"ok"``        -> return a valid live payload
    * ``"transient"`` -> raise a transient error (retryable)
    * ``"timeout"``   -> declare a latency larger than any sane deadline so the
                          executor's deadline check fires (classified timeout)
    * ``"hang"``      -> declare a normal latency and then really block, like a
                          socket with nobody on the other end. Nothing in the
                          declared value gives this away: only the wall-clock
                          watchdog stops it.
    * ``"malformed"`` -> return an off-contract dict (classified malformed_output)
"""

from __future__ import annotations

import threading

from ..classification import TransientToolError
from ..schemas import FlakyApiInput, FlakyApiOutput
from .base import Tool

_TIMEOUT_LATENCY_MS = 10_000.0  # 10s: guaranteed to blow any step deadline
_HANG_SECONDS = 30.0            # long enough that only the watchdog ends it


class FlakyApiTool(Tool[FlakyApiInput, FlakyApiOutput]):
    name = "flaky_api"
    input_model = FlakyApiInput
    output_model = FlakyApiOutput
    latency_ms = 20.0
    # A metered upstream: every attempt is billed, so a retry storm on this tool
    # is what the run cost ceiling exists to stop.
    cost_usd = 0.002

    def __init__(
        self,
        script: list[str] | None = None,
        *,
        cache: dict[str, str] | None = None,
    ) -> None:
        # Default script: fail transiently twice, then succeed.
        self._script = script or ["transient", "transient", "ok"]
        # A secondary/cached data source the fallback path can degrade to.
        self._cache = cache or {}
        # Never set. Waiting on it is how the "hang" behaviour blocks without
        # burning CPU; the watchdog abandons the thread it blocks.
        self._never_set = threading.Event()

    def _behavior(self, attempt: int) -> str:
        if not self._script:
            return "ok"
        idx = min(attempt, len(self._script) - 1)
        return self._script[idx]

    def next_latency_ms(self, request: FlakyApiInput, attempt: int = 0) -> float:
        if self._behavior(attempt) == "timeout":
            return _TIMEOUT_LATENCY_MS
        return self.latency_ms

    def run(self, request: FlakyApiInput, attempt: int = 0):
        beh = self._behavior(attempt)
        if beh == "transient":
            raise TransientToolError(
                f"flaky_api upstream 503 on attempt {attempt}",
                detail="temporary upstream failure",
            )
        if beh == "malformed":
            # Missing the required 'payload' field -> fails output validation.
            return {"resource": request.resource, "source": "live"}
        if beh == "timeout":
            # Should never actually reach here: the executor times out first.
            # Kept as a safety net so the contract is explicit.
            raise TransientToolError("unexpected: timeout attempt executed body")
        if beh == "hang":
            # A real block, with a declared latency of 20ms covering for it.
            # The watchdog stops waiting at the deadline and abandons this call.
            self._never_set.wait(_HANG_SECONDS)
        return FlakyApiOutput(
            resource=request.resource,
            payload=f"live-data::{request.resource}",
            source="live",
        )

    def cached(self, resource: str) -> FlakyApiOutput | None:
        """Secondary data source used by the orchestrator's fallback path."""
        payload = self._cache.get(resource)
        if payload is None:
            return None
        return FlakyApiOutput(resource=resource, payload=payload, source="cache")
