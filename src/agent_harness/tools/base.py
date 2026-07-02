"""Tool contract: typed input in, typed output out, validated at the boundary.

A :class:`Tool` binds a name to a pydantic input model and a pydantic output
model. The orchestrator never calls ``run`` directly with raw dicts — it goes
through :meth:`Tool.invoke`, which validates the input against the contract
*before* execution and validates the output *after*, converting any contract
breach into a classified :class:`HarnessError`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from ..classification import MalformedOutput, SchemaViolation

TIn = TypeVar("TIn", bound=BaseModel)
TOut = TypeVar("TOut", bound=BaseModel)


class Tool(ABC, Generic[TIn, TOut]):
    """Base class for every tool exposed to the orchestrator."""

    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]

    #: Declared, simulated execution latency in milliseconds. The executor uses
    #: this to advance the injected clock and to detect deadline overruns
    #: deterministically. Real tools would measure actual wall-time instead.
    latency_ms: float = 5.0

    @abstractmethod
    def run(self, request: TIn, attempt: int = 0) -> TOut | dict[str, Any]:
        """Execute the tool. May return the typed model or a raw dict.

        ``attempt`` is the 0-based retry attempt for this call, so a tool can
        legitimately behave differently across retries (the flaky API tool uses
        this to model an upstream that recovers on a later attempt).

        Returning a raw dict is deliberately allowed so we can model tools that
        emit *malformed* output — the boundary validation in :meth:`invoke`
        catches it and classifies it as ``malformed_output``.
        """

    def next_latency_ms(self, request: TIn, attempt: int = 0) -> float:
        """Declared latency for this specific call.

        The executor advances the injected clock by this amount and compares it
        against the step deadline to detect timeouts deterministically. Override
        to model a call whose latency varies (e.g. a hang on one attempt).
        """
        return self.latency_ms

    def validate_input(self, raw: dict[str, Any]) -> TIn:
        try:
            return self.input_model.model_validate(raw)  # type: ignore[return-value]
        except ValidationError as exc:
            raise SchemaViolation(
                f"input to '{self.name}' violated its schema",
                detail=_first_error(exc),
            ) from exc

    def validate_output(self, raw: Any) -> TOut:
        try:
            if isinstance(raw, self.output_model):
                return raw  # type: ignore[return-value]
            return self.output_model.model_validate(raw)  # type: ignore[return-value]
        except ValidationError as exc:
            raise MalformedOutput(
                f"output from '{self.name}' did not match its contract",
                detail=_first_error(exc),
            ) from exc

    def invoke(self, raw_args: dict[str, Any], attempt: int = 0) -> TOut:
        """Full validated round-trip: validate in -> run -> validate out."""
        request = self.validate_input(raw_args)
        result = self.run(request, attempt)
        return self.validate_output(result)

    def input_json_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def output_json_schema(self) -> dict[str, Any]:
        return self.output_model.model_json_schema()


def _first_error(exc: ValidationError) -> str:
    errs = exc.errors()
    if not errs:
        return str(exc)
    e = errs[0]
    loc = ".".join(str(p) for p in e.get("loc", ()))
    return f"{loc}: {e.get('msg', 'invalid')}"
