"""Render a JSONL trace into a self-contained dark-theme HTML report.

No CDN, no external assets — all CSS is inlined so the file opens offline from
disk. The report shows the run header, a per-step timeline, and colour-coded
badges for outcomes and failure classifications.
"""

from __future__ import annotations

import html
from pathlib import Path

from .trace import TraceEvent, load_events

_EVENT_LABEL = {
    "plan": "PLAN",
    "tool_call": "CALL",
    "tool_result": "RESULT",
    "retry": "RETRY",
    "safety_block": "BLOCKED",
    "classify": "CLASSIFY",
    "fallback": "FALLBACK",
    "limit": "LIMIT",
    "final": "FINAL",
}

# Colours per outcome/classification (dark theme).
_OUTCOME_COLORS = {
    "ok": "#3fb950",
    "error": "#f85149",
    "blocked": "#d29922",
    "degraded": "#d29922",
    "failed": "#f85149",
    "halted": "#f85149",
}
_CLASS_COLORS = {
    "malformed_output": "#f85149",
    "missing_context": "#a371f7",
    "timeout": "#d29922",
    "unsafe_action": "#ff7b72",
    "transient": "#58a6ff",
    "schema_violation": "#db61a2",
    "cost_limit": "#ffa657",
    "step_limit": "#ffa657",
}


def _badge(text: str, color: str) -> str:
    return (
        f'<span class="badge" style="background:{color}22;color:{color};'
        f'border:1px solid {color}55">{html.escape(text)}</span>'
    )


def _row(evt: TraceEvent, t0: float) -> str:
    label = _EVENT_LABEL.get(evt.event, evt.event.upper())
    rel = evt.ts - t0
    outcome = ""
    if evt.outcome:
        outcome = _badge(evt.outcome, _OUTCOME_COLORS.get(evt.outcome, "#8b949e"))
    classification = ""
    if evt.classification:
        classification = _badge(
            evt.classification, _CLASS_COLORS.get(evt.classification, "#8b949e")
        )
    latency = f"{evt.latency_ms:.0f}ms" if evt.latency_ms is not None else ""
    attempt = f"#{evt.attempt}" if evt.attempt is not None else ""
    tool = html.escape(evt.tool or "")
    detail = html.escape(evt.detail or "")
    args = ""
    if evt.args:
        args = html.escape(", ".join(f"{k}={v}" for k, v in evt.args.items()))
        args = f'<div class="args">{args}</div>'

    return f"""
      <tr class="ev ev-{html.escape(evt.event)}">
        <td class="col-t">+{rel:.3f}s</td>
        <td class="col-step">{evt.step_id}{(' ' + attempt) if attempt else ''}</td>
        <td class="col-ev"><span class="evlabel">{label}</span></td>
        <td class="col-tool">{tool}</td>
        <td class="col-badges">{outcome} {classification} <span class="lat">{latency}</span></td>
        <td class="col-detail">{detail}{args}</td>
      </tr>"""


def render_html(events: list[TraceEvent]) -> str:
    if not events:
        body = "<p>No events.</p>"
        run_id = "(empty)"
        header = ""
    else:
        run_id = events[0].run_id
        t0 = events[0].ts
        final = next((e for e in reversed(events) if e.event == "final"), None)
        status = (final.outcome if final else "unknown") or "unknown"
        status_color = _OUTCOME_COLORS.get(status, "#8b949e")
        counts = (final.args if final else {}) or {}
        header = f"""
        <div class="summary">
          <div>run <code>{html.escape(run_id)}</code></div>
          <div>status {_badge(status, status_color)}</div>
          <div>ok {counts.get('steps_ok', 0)} &middot;
               degraded {counts.get('steps_degraded', 0)} &middot;
               failed {counts.get('steps_failed', 0)} &middot;
               not attempted {counts.get('steps_skipped', 0)}</div>
          <div>spend ${float(counts.get('cost_usd', 0.0) or 0.0):.4f}</div>
          <div>{len(events)} trace events</div>
        </div>
        <div class="answer">{html.escape(final.detail if final else '')}</div>"""
        rows = "\n".join(_row(e, t0) for e in events)
        body = f"""
        <table>
          <thead><tr>
            <th>t</th><th>step</th><th>event</th><th>tool</th>
            <th>outcome / class</th><th>detail</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>"""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent harness trace: {html.escape(run_id)}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 2rem;
    background: #0d1117; color: #c9d1d9;
    font: 14px/1.5 ui-monospace, SFMono-Regular, "Cascadia Code", Consolas, monospace;
  }}
  h1 {{ font-size: 1.15rem; margin: 0 0 .25rem; color: #e6edf3; }}
  .sub {{ color: #8b949e; margin: 0 0 1.5rem; }}
  .summary {{ display: flex; gap: 1.5rem; flex-wrap: wrap;
    padding: 1rem; background: #161b22; border: 1px solid #30363d;
    border-radius: 10px; margin-bottom: 1rem; }}
  .answer {{ padding: .75rem 1rem; background: #161b22;
    border-left: 3px solid #3fb950; border-radius: 6px; margin-bottom: 1.5rem;
    color: #e6edf3; }}
  code {{ color: #58a6ff; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; color: #8b949e; font-weight: 600;
    border-bottom: 1px solid #30363d; padding: .5rem; position: sticky; top: 0;
    background: #0d1117; }}
  td {{ padding: .5rem; border-bottom: 1px solid #21262d; vertical-align: top; }}
  .col-t {{ color: #8b949e; white-space: nowrap; }}
  .col-step {{ color: #8b949e; white-space: nowrap; }}
  .evlabel {{ font-weight: 700; color: #e6edf3; }}
  .ev-retry {{ background: #d2992210; }}
  .ev-safety_block, .ev-classify {{ background: #f8514910; }}
  .ev-fallback {{ background: #a371f710; }}
  .ev-limit {{ background: #ffa65718; }}
  .ev-final {{ background: #3fb95010; }}
  .badge {{ display: inline-block; padding: .05rem .45rem; border-radius: 999px;
    font-size: .78rem; font-weight: 600; margin-right: .25rem; }}
  .lat {{ color: #8b949e; font-size: .8rem; }}
  .col-tool {{ color: #79c0ff; }}
  .args {{ color: #8b949e; font-size: .82rem; margin-top: .15rem; }}
  .col-detail {{ max-width: 46ch; }}
</style></head>
<body>
  <h1>Agent orchestration reliability harness, trace report</h1>
  <p class="sub">Deterministic, offline. Rendered from JSONL; no external assets.</p>
  {header}
  {body}
</body></html>"""


def render_file(trace_path: Path | str, out_path: Path | str) -> Path:
    events = load_events(trace_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(events), encoding="utf-8")
    return out_path
