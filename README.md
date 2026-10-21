# agent-harness

## The problem

An agent that calls outside tools can hang on a slow service, retry forever,
keep spending money, loop through a long plan, or pass a destructive command to
a tool. When that happens, the operator often cannot see which step went wrong
or why.

## How it solves it

- **Real deadlines.** Each tool call runs on a worker thread, and the harness
  stops waiting when the step deadline passes. The deadline is checked against
  measured time, not against what the tool says its latency will be, so a tool
  that under-reports its latency or hangs outright is cut off.
- **Named failure types.** Every failure is sorted into a named type (listed
  under Architecture), and that type decides what happens next. Only temporary failures and timeouts are
  retried, with a capped exponential backoff. When retries run out, the step
  falls back to a cached source or returns a safe refusal, and the run keeps
  going.
- **Spend and step limits, on by default.** A run stops when it reaches its
  cost ceiling (default $1.00) or its step ceiling (default 20). Every attempt
  is charged, retries included, so a retry storm reaches the ceiling.
- **A safety gate before execution.** Tool arguments are checked for path
  traversal, Python code-execution probes, `rm -rf`, `DROP TABLE` and
  `; shutdown`. Delete and write actions are blocked unless the resource is on
  an allow list. A blocked call is never retried.
- **A trace of every step.** Each plan, call, result, retry, block,
  classification, fallback, limit and final answer is written as one JSON line,
  and an HTML report is rendered from that file.

## Who it helps

- Engineers building agent loops who need time and cost limits that hold even
  when a tool misbehaves.
- Teams calling unreliable or paid APIs who want retries limited to failures
  that are worth retrying, and a record of what each run spent.

## Technical details

- **Language and dependencies.** Python 3.11 or later. The only runtime
  dependency is `pydantic`. `pytest` is used for tests.
- **Offline and repeatable.** No API keys and no network. A fixture-driven
  planner drives the loop, and timing flows through an injected clock, so tests
  can assert exact timing.
- **Unimplemented LLM planner hook.** `LLMPlanner` in `planner.py` is a stub.
  It requires `AGENT_HARNESS_LLM=1` plus `ANTHROPIC_API_KEY`, then raises
  `NotImplementedError`; a real model client has to be wired in there. The demos
  and tests use the fixture-driven `ScriptedPlanner`.
- **Tests.** The tests check that hangs and under-reported latencies are cut off
  at the deadline, that the cost and step ceilings halt a run, that only
  retryable failures are retried, that unsafe calls are blocked before they
  run, and that traces and reports are written correctly. The table under
  [Tests](#tests) maps each failure case to its test.
- **CI.** GitHub Actions on Python 3.11, 3.12 and 3.13 runs the tests, the four
  demo scenarios and the report renderer.

## Start here

Read one recorded failure first:
[`trajectories/real-hang-recovery.md`](trajectories/real-hang-recovery.md). It
is a single run that goes wrong: a tool hangs and is cut off at its deadline,
the step recovers on a later attempt, and the run then stops itself on its spend
ceiling with part of its plan unexecuted. Nothing needs to be run to read it.

## Quick start

```bash
git clone https://github.com/dbhavery/agent-harness
cd agent-harness
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install -e ".[dev]"        # installs pydantic and pytest

# run the demo scenarios
python -m agent_harness demo flaky_recovery       # flaky tool, classified transient, retried, recovers
python -m agent_harness demo timeout_then_cache   # hang, timeout, cache fallback
python -m agent_harness demo real_hang_recovery   # real hang, watchdog cutoff, recovery, cost ceiling halt
python -m agent_harness demo unsafe_blocked       # destructive action blocked by the safety gate
python -m agent_harness scenarios                 # list scenarios

# run the tests
pytest -q

# open the trace report (rewritten by every demo run)
#   Windows:
start reports/trace-report.html
#   macOS:
open reports/trace-report.html
#   Linux:
xdg-open reports/trace-report.html

# render a report from any trace file
python -m agent_harness report traces/demo-flaky_recovery.jsonl
```

Without an editable install, everything also runs with `PYTHONPATH=src` (for
example `PYTHONPATH=src python -m agent_harness demo flaky_recovery`).

## Architecture

```
 goal
  │
  ▼
┌────────────┐   Plan (typed steps)   ┌───────────────────────────────────────┐
│  Planner   │ ─────────────────────▶ │             Orchestrator              │
│ (scripted; │                        │  for each step:                       │
│  LLM stub) │                        │   1. Safety gate  ── block ─┐         │
└────────────┘                        │   2. Executor (validate in) │         │
                                       │   3. Retry policy (backoff) │         │
   ┌───────────────┐                   │   4. Fallback / safe refuse │         │
   │  Tools        │◀── typed I/O ────▶│   5. Emit trace at each step│         │
   │  doc_search   │                   └──────────────┬──────────────┘         │
   │  calc_units   │                                  │                        │
   │  flaky_api    │                                  ▼                        │
   └───────────────┘                          ┌──────────────┐                 │
                                              │  Trace bus   │──▶ traces/*.jsonl │
                                              └──────┬───────┘                 │
                                                     ▼                         │
                                              reports/trace-report.html ◀──────┘
```

- **Typed tools** (`src/agent_harness/schemas.py`, `tools/`). Each tool binds a
  name to a pydantic input model and output model, and every call is validated
  before execution and after, so a contract breach is reported as a classified
  error instead of a crash. Three tools ship: `doc_search` (in-memory corpus),
  `calc_units` (arithmetic and unit conversion, no `eval`), and `flaky_api` (a
  scripted, deliberately unreliable upstream).
- **Orchestrator** (`orchestrator.py`). A plan, safety gate, execute, observe,
  respond loop, with timing taken from an injected `Clock` (`clock.py`) so a
  scripted run is reproducible.
- **Watchdog** (`watchdog.py`). Every call runs on a worker thread and the wait
  stops at the step deadline, measured in real time, and a hung call is abandoned
  rather than joined. A tool's declared latency is kept as trace metadata and
  decides nothing.
- **Run ceilings** (`limits.py`). `RunLimits` caps spend (`max_cost_usd`,
  default 1.00) and steps (`max_steps`, default 20), using the price each tool
  sets in `Tool.cost_usd`. Hitting a ceiling halts the run with no fallback:
  `RunResult.halted`, `halt_reason`, `steps_skipped` and `cost_usd` say what
  stopped the run and what was never attempted.
- **Trace bus** (`trace.py`). Every step (plan, tool_call, tool_result, retry,
  safety_block, classify, fallback, limit, final) is one `TraceEvent` appended to
  `traces/<run_id>.jsonl`, and both the report and the tests read that file.
- **Retry and fallback** (`retry.py`). Bounded retries with capped exponential
  backoff, only for the `transient` and `timeout` classes. When retries are
  exhausted, the orchestrator uses a secondary or cached source, or returns a
  safe refusal.
- **Failure classification** (`classification.py`). The types are
  `malformed_output`, `missing_context`, `timeout`, `unsafe_action`,
  `transient`, `schema_violation`, `cost_limit`, `step_limit`. Retry, fallback
  and halt decisions are driven by these types.
- **Safety gate** (`safety.py`). Runs before execution. Blocks destructive
  actions on resources that are not allow-listed, and blocks traversal,
  code-execution and destructive-command patterns in arguments. Blocks are
  classified `unsafe_action` and never retried.

## Sample output

`python -m agent_harness demo flaky_recovery` (timings vary from run to run):

```
+ 0.014s  step 3  TOOL_CALL     flaky_api   deadline=500ms
+ 0.034s  step 3  RETRY         flaky_api   [transient]  error  50ms  attempt 0 failed (temporary upstream failure); backing off 50ms
+ 0.105s  step 3  RETRY         flaky_api   [transient]  error  100ms  attempt 1 failed (temporary upstream failure); backing off 100ms
+ 0.226s  step 3  TOOL_RESULT   flaky_api   ok  21ms  flaky_api: order-8841 -> live-data::order-8841 [live] [measured 21ms, declared 20ms]
```

The `timeout_then_cache` trace shows a hang cut off at the deadline and the
cache fallback:

```jsonl
{"event":"retry","classification":"timeout","detail":"attempt 0 failed (measured 500ms >= budget 500ms (tool declared 10000ms)); backing off 50ms",...}
{"event":"classify","classification":"timeout","detail":"retries exhausted / non-retryable: measured 500ms >= budget 500ms (tool declared 10000ms)",...}
{"event":"fallback","classification":"timeout","outcome":"degraded","detail":"served stale cache for 'order-8841' after timeout",...}
```

In `real_hang_recovery` the tool declares a normal 20ms latency and then
blocks, so nothing it reports gives the hang away and only the watchdog ends the
call. The run recovers on a later attempt and then halts on its spend ceiling.
The full run, with its raw trace, is in
[`trajectories/real-hang-recovery.md`](trajectories/real-hang-recovery.md).

`screenshots/` holds raw terminal captures and a screenshot of the HTML trace
report.

## Tests

Run with `pytest -q`. Each failure case has a named test:

| Failure case | Test | What it checks |
|---|---|---|
| malformed output | `test_malformed_output_classified_and_not_infinite` | classified `malformed_output`, not retried, no crash |
| missing context | `test_missing_context_handled_not_crashed` | classified `missing_context`, handled |
| timeout (hang) | `test_timeout_is_bounded_and_degrades_to_cache` | bounded by deadline, classified `timeout`, cache fallback |
| a tool that under-reports its latency | `test_tool_that_underreports_its_latency_is_cut_off` | declared 10ms, slept 1.5s, cut off at the 500ms deadline |
| a tool that hangs silently | `test_tool_that_hangs_and_reports_nothing_is_cut_off` | no declared latency at all, still bounded |
| measured timing | `test_successful_call_is_traced_with_its_measured_duration` | the trace records measured time, not the tool's claim |
| unbounded spend | `test_run_halts_when_the_cost_ceiling_is_reached` | run halts at the ceiling, `cost_limit`, rest of plan unexecuted |
| retry spend | `test_retry_attempts_are_charged_against_the_ceiling` | every attempt is billed, so a retry storm hits the ceiling |
| runaway loop | `test_forty_step_plan_halts_at_an_explicit_step_ceiling` | 40-step plan stops at 12, `step_limit` |
| ceiling by default | `test_the_step_ceiling_is_on_by_default` | no configuration needed for the ceiling to fire |
| unsafe action | `test_unsafe_action_blocked_by_gate` | blocked before execution, logged, `unsafe_action` |
| recovery | `test_transient_failures_then_recover` | retries with backoff, then succeeds |
| schema violation | `test_schema_violation_at_input` | rejected at the typed boundary |
| safe degrade | `test_timeout_without_cache_is_safe_refusal` | no secondary source, safe refusal, run stays alive |
| the published trajectory | `test_real_hang_scenario_is_cut_off_recovers_then_halts_on_cost` | the run written up in `trajectories/` still behaves that way |

There are also unit tests for the retry policy, backoff math, classifier, safety
patterns, trace persistence and determinism, and report rendering.

## Limitations

- **Not a production service.** There is no server, no persistence layer and no
  auth.
- **The flaky API is simulated.** Its failures (transient, timeout, hang,
  malformed) are scripted per attempt so the demo is repeatable. Two of them
  model slowness in different ways: `timeout` declares a latency past the
  deadline, and `hang` declares a normal latency and then blocks on an event
  nobody sets. The enforcement path is the same one a real slow call would hit,
  since the watchdog measures elapsed time and cannot tell a blocked event from
  a dead socket, but the slowness itself is injected, not a real network fault.
- **The tool prices are placeholders.** `doc_search` at $0.0002 a query and
  `flaky_api` at $0.002 a call are made up. The per-attempt charging, the ledger
  arithmetic and the halt are real.
- **No LLM is called.** The shipped planner is fixture-driven, and
  `LLMPlanner` is an unimplemented hook, as described above.

## License

MIT. See `LICENSE`.
