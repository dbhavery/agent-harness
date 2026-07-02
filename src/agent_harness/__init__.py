"""Agent orchestration reliability harness.

A small, inspectable agent workflow that demonstrates surviving real
operational edge cases: typed tool contracts, a deterministic orchestrator,
structured JSONL traces, bounded retry with fallback, failure classification,
and a safety gate.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .classification import FailureClass
from .orchestrator import Orchestrator
from .retry import RetryPolicy
from .safety import SafetyGate
from .schemas import Plan, RunResult, ToolStep

__all__ = [
    "FailureClass",
    "Orchestrator",
    "RetryPolicy",
    "SafetyGate",
    "Plan",
    "RunResult",
    "ToolStep",
    "__version__",
]
