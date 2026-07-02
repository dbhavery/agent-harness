"""Safety / validation gate.

Runs *before* a tool executes. It inspects the proposed (tool, args) pair and
either allows it or blocks it with a reason. Blocks are classified as
``unsafe_action`` and are never retried — retrying a disallowed action would
just re-block. This is independent of pydantic schema validation: an action can
be perfectly well-typed and still be unsafe (e.g. a destructive delete).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Substrings that should never appear in free-text tool arguments. These model
# injection / traversal / code-exec attempts reaching a tool.
_DANGEROUS_PATTERNS = [
    re.compile(r"\.\./"),                 # path traversal
    re.compile(r"__import__|__builtins__"),  # python code-exec probes
    re.compile(r"\brm\s+-rf\b", re.I),    # shell wipe
    re.compile(r"\bDROP\s+TABLE\b", re.I),  # sql drop
    re.compile(r";\s*shutdown", re.I),
]

# Destructive actions are gated per tool. Only allow-listed resources may be
# targeted by a destructive action.
_DESTRUCTIVE_ACTIONS = {"delete", "write"}


@dataclass(slots=True)
class SafetyDecision:
    allowed: bool
    reason: str
    rule: str

    @classmethod
    def allow(cls) -> "SafetyDecision":
        return cls(allowed=True, reason="ok", rule="none")

    @classmethod
    def block(cls, reason: str, rule: str) -> "SafetyDecision":
        return cls(allowed=False, reason=reason, rule=rule)


class SafetyGate:
    def __init__(self, *, delete_allowlist: set[str] | None = None) -> None:
        # Resources for which destructive actions are explicitly permitted.
        self._delete_allowlist = delete_allowlist or set()

    def check(self, tool_name: str, args: dict[str, Any]) -> SafetyDecision:
        # 1. Free-text injection / traversal / code-exec scan on all string args.
        for key, value in _iter_strings(args):
            for pat in _DANGEROUS_PATTERNS:
                if pat.search(value):
                    return SafetyDecision.block(
                        reason=f"arg '{key}' matched forbidden pattern /{pat.pattern}/",
                        rule="dangerous_pattern",
                    )

        # 2. Destructive-action gating for the external API tool.
        if tool_name == "flaky_api":
            action = str(args.get("action", "read"))
            resource = str(args.get("resource", ""))
            if action in _DESTRUCTIVE_ACTIONS and resource not in self._delete_allowlist:
                return SafetyDecision.block(
                    reason=f"destructive action '{action}' on non-allowlisted "
                    f"resource '{resource}'",
                    rule="destructive_not_allowlisted",
                )

        return SafetyDecision.allow()


def _iter_strings(obj: Any, prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(obj, str):
        out.append((prefix or "<root>", obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_iter_strings(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.extend(_iter_strings(v, f"{prefix}[{i}]"))
    return out
