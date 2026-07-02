"""Typed I/O contracts for every tool, plus the run-level request/response.

These pydantic models are the explicit, validated contracts referenced by the
architecture section of the README. Keeping them in one module makes the whole
tool surface inspectable at a glance.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------------------- #
# doc_search                                                                   #
# --------------------------------------------------------------------------- #


class DocSearchInput(BaseModel):
    model_config = {"extra": "forbid"}

    query: str = Field(..., min_length=1, description="Free-text search query.")
    top_k: int = Field(3, ge=1, le=10, description="Max number of hits to return.")

    @field_validator("query")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query must not be blank")
        return v


class DocHit(BaseModel):
    doc_id: str
    title: str
    snippet: str
    score: float = Field(..., ge=0.0, le=1.0)


class DocSearchOutput(BaseModel):
    model_config = {"extra": "forbid"}

    results: list[DocHit]


# --------------------------------------------------------------------------- #
# calc_units                                                                   #
# --------------------------------------------------------------------------- #

Op = Literal["add", "sub", "mul", "div", "convert"]


class CalcInput(BaseModel):
    model_config = {"extra": "forbid"}

    op: Op = Field(..., description="Arithmetic op, or 'convert' for unit conversion.")
    operands: list[float] = Field(default_factory=list)
    #: For 'convert': the source unit. For arithmetic: an optional label.
    from_unit: str | None = None
    to_unit: str | None = None
    value: float | None = Field(
        None, description="The scalar to convert (used by op='convert')."
    )


class CalcOutput(BaseModel):
    model_config = {"extra": "forbid"}

    value: float
    unit: str | None = None


# --------------------------------------------------------------------------- #
# flaky_api                                                                    #
# --------------------------------------------------------------------------- #


class FlakyApiInput(BaseModel):
    model_config = {"extra": "forbid"}

    resource: str = Field(..., min_length=1, description="Logical resource id to fetch.")
    action: Literal["read", "write", "delete"] = "read"


class FlakyApiOutput(BaseModel):
    model_config = {"extra": "forbid"}

    resource: str
    payload: str
    source: Literal["live", "cache"] = "live"


# --------------------------------------------------------------------------- #
# Run-level contracts                                                          #
# --------------------------------------------------------------------------- #


class ToolStep(BaseModel):
    """One planned step the orchestrator will execute."""

    tool: str
    args: dict = Field(default_factory=dict)
    #: Optional per-step deadline in ms; overrides the orchestrator default.
    timeout_ms: float | None = None


class Plan(BaseModel):
    goal: str
    steps: list[ToolStep]


class RunResult(BaseModel):
    run_id: str
    goal: str
    status: Literal["ok", "degraded", "failed"]
    answer: str
    steps_ok: int
    steps_degraded: int
    steps_failed: int
    failure_classes: list[str] = Field(default_factory=list)
    trace_path: str | None = None
