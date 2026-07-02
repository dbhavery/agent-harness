"""Command-line entrypoint: ``python -m agent_harness <command>``.

Commands:
  demo [scenario]     Run a scenario end-to-end, print a readable trace, and
                      regenerate the HTML report. Default scenario is
                      'flaky_recovery' (hits a flaky tool, retries, recovers).
  scenarios           List available scenarios.
  report <trace.jsonl>  Render an existing JSONL trace to reports/trace-report.html.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .clock import SystemClock
from .report import render_file
from .scenarios import SCENARIOS, build_harness
from .trace import (
    CLASSIFY,
    FALLBACK,
    RETRY,
    SAFETY_BLOCK,
    load_events,
)

ROOT = Path(__file__).resolve().parents[2]
TRACES_DIR = ROOT / "traces"
REPORTS_DIR = ROOT / "reports"

# ANSI colours (fall back to no-colour if not a TTY).
_C = {
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def _c(text: str, color: str, enable: bool) -> str:
    if not enable:
        return text
    return f"{_C[color]}{text}{_C['reset']}"


_EVENT_COLOR = {
    "plan": "cyan",
    "tool_call": "blue",
    "tool_result": "green",
    RETRY: "yellow",
    SAFETY_BLOCK: "red",
    CLASSIFY: "red",
    FALLBACK: "magenta",
    "final": "bold",
}


def _print_trace(trace_path: Path, color: bool) -> None:
    events = load_events(trace_path)
    t0 = events[0].ts if events else 0.0
    for e in events:
        rel = e.ts - t0
        label = e.event.upper().ljust(12)
        label = _c(label, _EVENT_COLOR.get(e.event, "reset"), color)
        parts = [f"{_c(f'+{rel:6.3f}s', 'dim', color)}", f"step{e.step_id:>2}", label]
        if e.tool:
            parts.append(_c(e.tool.ljust(10), "cyan", color))
        if e.classification:
            parts.append(_c(f"[{e.classification}]", "red", color))
        if e.outcome:
            oc = {"ok": "green", "error": "red", "blocked": "red", "degraded": "yellow", "failed": "red"}.get(e.outcome, "reset")
            parts.append(_c(e.outcome, oc, color))
        if e.latency_ms is not None:
            parts.append(_c(f"{e.latency_ms:.0f}ms", "dim", color))
        if e.detail:
            parts.append(_c(e.detail, "dim", color))
        print("  ".join(parts))


def cmd_demo(args: argparse.Namespace) -> int:
    scenario = args.scenario
    color = sys.stdout.isatty() and not args.no_color
    if scenario not in SCENARIOS:
        print(f"unknown scenario '{scenario}'. choices: {', '.join(SCENARIOS)}")
        return 2

    clock = SystemClock(start=0.0)
    orch, plan = build_harness(scenario, clock, trace_dir=TRACES_DIR)

    print(_c("=" * 72, "dim", color))
    print(_c(f" agent-harness demo :: scenario '{scenario}'", "bold", color))
    print(_c(f" goal: {plan.goal}", "dim", color))
    print(_c("=" * 72, "dim", color))
    print()

    result = orch.run(plan, run_id=f"demo-{scenario}")

    print()
    print(_c("--- trace " + "-" * 62, "dim", color))
    _print_trace(Path(result.trace_path), color)
    print(_c("-" * 72, "dim", color))
    print()

    status_color = {"ok": "green", "degraded": "yellow", "failed": "red"}[result.status]
    print(f"  status:          {_c(result.status.upper(), status_color, color)}")
    print(f"  steps ok/deg/fail: {result.steps_ok}/{result.steps_degraded}/{result.steps_failed}")
    if result.failure_classes:
        print(f"  classifications: {_c(', '.join(result.failure_classes), 'yellow', color)}")
    print(f"  answer:          {result.answer}")
    print(f"  trace:           {result.trace_path}")

    report_path = render_file(result.trace_path, REPORTS_DIR / "trace-report.html")
    print(f"  report:          {report_path}")
    return 0


def cmd_scenarios(_args: argparse.Namespace) -> int:
    for s in SCENARIOS:
        print(s)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    out = render_file(args.trace, REPORTS_DIR / "trace-report.html")
    print(f"wrote {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent_harness", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("demo", help="run a scenario end-to-end")
    d.add_argument("scenario", nargs="?", default="flaky_recovery")
    d.add_argument("--no-color", action="store_true")
    d.set_defaults(func=cmd_demo)

    s = sub.add_parser("scenarios", help="list scenarios")
    s.set_defaults(func=cmd_scenarios)

    r = sub.add_parser("report", help="render a JSONL trace to HTML")
    r.add_argument("trace", help="path to a .jsonl trace file")
    r.set_defaults(func=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
