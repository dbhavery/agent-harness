"""Run-level ceilings: the limits that stop a run rather than shape a step.

A retry policy bounds one step. It says nothing about a run that keeps finding
new steps to take, or that keeps paying for attempts. Those need ceilings on the
run itself, and they need to be on by default, because a limit you have to
remember to set is not a limit.

:class:`RunLimits` holds the ceilings. :class:`CostLedger` is the per-run meter
the executor charges every attempt against, retries included, since retries are
how a harness quietly spends money.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Default spend ceiling for one run, in US dollars.
DEFAULT_MAX_COST_USD = 1.00

#: Default ceiling on how many plan steps one run may execute. Set above the
#: longest shipped plan and far below a runaway loop.
DEFAULT_MAX_STEPS = 20


@dataclass(slots=True)
class RunLimits:
    """Ceilings applied to a whole run. ``None`` disables a ceiling."""

    max_cost_usd: float | None = DEFAULT_MAX_COST_USD
    max_steps: int | None = DEFAULT_MAX_STEPS

    def __post_init__(self) -> None:
        if self.max_cost_usd is not None and self.max_cost_usd < 0:
            raise ValueError("max_cost_usd cannot be negative")
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")


@dataclass(slots=True)
class CostLedger:
    """Tracks what a run has spent and whether the next charge is affordable.

    Every attempt is charged *before* the call, because a metered endpoint bills
    for a request that times out just as it bills for one that answers.
    """

    max_cost_usd: float | None = None
    spent_usd: float = 0.0
    charges: int = 0
    #: (tool name, usd) per charge, so a trace can be reconciled line by line.
    entries: list[tuple[str, float]] = field(default_factory=list)

    def would_exceed(self, usd: float) -> bool:
        if self.max_cost_usd is None:
            return False
        # Cents, rounded, so float arithmetic cannot drift a run over its own
        # ceiling by 1e-17.
        return round(self.spent_usd + usd, 10) > round(self.max_cost_usd, 10)

    def charge(self, tool_name: str, usd: float) -> float:
        if usd < 0:
            raise ValueError("a charge cannot be negative")
        self.spent_usd = round(self.spent_usd + usd, 10)
        self.charges += 1
        self.entries.append((tool_name, usd))
        return self.spent_usd

    @property
    def remaining_usd(self) -> float | None:
        if self.max_cost_usd is None:
            return None
        return round(self.max_cost_usd - self.spent_usd, 10)
