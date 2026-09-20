# Published trajectory: a real hang, a recovery, and a spend ceiling

One recorded run. Nothing here needs to be executed to be read: the terminal
capture, the raw JSONL trace and the rendered HTML report in this folder all come
from the same run, on 2026-09-19, of

    python -m agent_harness demo real_hang_recovery

A harness that only ever shows green is not evidence, so this is the run that
goes wrong. It contains a tool that hangs for real, a recovery, and a run that
stops itself on a spend ceiling with part of its plan unexecuted.

Files:

| File | What it is |
|---|---|
| `real-hang-recovery.md` | this walkthrough |
| `real-hang-recovery.jsonl` | the raw trace, one JSON object per event |
| `real-hang-recovery.html` | the rendered report, offline, no external assets |

Reproduce it with `python -m agent_harness demo real_hang_recovery`. The
millisecond numbers are real wall-clock time and will differ by a few ms per run;
the sequence of events will not.

---

## The plan

Four steps, a $0.01 ceiling for the whole run, a 500ms deadline per step, and at
most three attempts per step:

```
doc_search   query="timeout deadline watchdog"
flaky_api    resource=order-8841
flaky_api    resource=order-9002
calc_units   1 + 1
```

`flaky_api` is a scripted upstream. Its script for this scenario is
`["hang", "transient", "ok"]` and it restarts for each step, so both flaky steps
begin by hanging. Prices: `doc_search` $0.0002 per query, `flaky_api` $0.002 per
attempt, `calc_units` free.

## What happened

```
+ 0.000s  step 0  PLAN          goal='Survive an upstream that really hangs, recover on retry, then stop at the run cost ceiling.'; 4 step(s)
+ 0.000s  step 1  TOOL_CALL     doc_search  deadline=500ms
+ 0.010s  step 1  TOOL_RESULT   doc_search  ok  10ms  doc_search: top hit 'Timeout handling' (score 0.6667) [measured 10ms, declared 8ms]
+ 0.010s  step 2  TOOL_CALL     flaky_api   deadline=500ms
+ 0.520s  step 2  RETRY         flaky_api   [timeout]  error  50ms  attempt 0 failed (measured 500ms with the call still running; watchdog abandoned it); backing off 50ms
+ 0.591s  step 2  RETRY         flaky_api   [transient]  error  100ms  attempt 1 failed (temporary upstream failure); backing off 100ms
+ 0.712s  step 2  TOOL_RESULT   flaky_api   ok  20ms  flaky_api: order-8841 -> live-data::order-8841 [live] [measured 20ms, declared 20ms]
+ 0.712s  step 3  TOOL_CALL     flaky_api   deadline=500ms
+ 1.227s  step 3  RETRY         flaky_api   [timeout]  error  50ms  attempt 0 failed (measured 500ms with the call still running; watchdog abandoned it); backing off 50ms
+ 1.278s  step 3  CLASSIFY      flaky_api   [cost_limit]  error  retries exhausted / non-retryable: spent $0.0082 over 5 charge(s); next call costs $0.0020; ceiling $0.01
+ 1.278s  step 3  LIMIT         flaky_api   [cost_limit]  halted  run halted: spent $0.0082 over 5 charge(s); next call costs $0.0020; ceiling $0.01
+ 1.278s  step 4  FINAL         degraded  doc_search: top hit 'Timeout handling' (score 0.6667) | flaky_api: order-8841 -> live-data::order-8841 [live] | run halted at cost_limit: spent $0.0082 over 5 charge(s); next call costs $0.0020; ceiling $0.01 | 1 planned step(s) not attempted

  status:          DEGRADED
  steps ok/deg/fail: 2/0/1
  spend:           $0.0082 of $0.01 ceiling
  halted:          cost_limit: spent $0.0082 over 5 charge(s); next call costs $0.0020; ceiling $0.01
  not attempted:   1 planned step(s)
  classifications: cost_limit
```

## The failure: a call that hangs and says nothing about it

Step 2 attempt 0 called a tool that declared a 20ms latency and then blocked on
an event nobody sets. No declared value gives that away, which is the point. The
run waited out its 500ms budget, abandoned the call, and classified it `timeout`:

```json
{"event":"retry","tool":"flaky_api","classification":"timeout","attempt":0,"latency_ms":50.0,
 "detail":"attempt 0 failed (measured 500ms with the call still running; watchdog abandoned it); backing off 50ms"}
```

Read the timestamps: the call started at +0.010s and the retry was recorded at
+0.520s. Half a second of real time, which is the budget, not the 30 seconds the
tool intended to block for and not the 20ms it claimed. The abandoned worker
thread is a daemon, so it holds nothing open, and its eventual result is dropped.

Until 2026-09-19 this step would have returned `ok`. The executor compared the
tool's own declared latency against the budget and never timed the call, so a
tool that under-reported its latency ran as long as it liked. The audit that
found it is in the repo history; the tests that pin it are
`tests/test_watchdog.py`.

## The recovery

Attempt 1 failed differently: an ordinary transient 503, classified `transient`,
backed off 100ms. Attempt 2 answered in 20ms and step 2 came back `ok` with live
data. Two failures of two different kinds, one usable answer, no crash, and each
decision on the record.

Note the two backoffs, 50ms and 100ms: exponential from a 50ms base, bounded, and
only applied to classes that are worth retrying.

## The second control: the run stops spending

Step 3 hung the same way and was cut off the same way. Its retry never ran. By
then the run had made five billable calls, $0.0082 of its $0.01 ceiling, and the
next attempt would have passed it:

```json
{"event":"limit","tool":"flaky_api","classification":"cost_limit","outcome":"halted",
 "detail":"run halted: spent $0.0082 over 5 charge(s); next call costs $0.0020; ceiling $0.01"}
```

Five charges for three steps, because every attempt bills: one search, three
attempts on step 2, one attempt on step 3. A timed-out call is charged like any
other, since a metered endpoint bills for a request whether or not it answers.

The halt has no fallback path. The cache holds an entry for `order-8841` and the
harness did not reach for it, because serving a cached answer here and then
carrying on to the next paid step is exactly what the ceiling exists to prevent.
The fourth step, `calc_units`, was never attempted, and the result says so rather
than reporting a complete run.

## What a reviewer should take from it

- The deadline is enforced by the harness against measured wall-clock time, not
  by trusting what a tool reports about itself.
- A failure is classified before it is acted on, and the class decides whether a
  retry happens at all.
- Two different failures in one step still produced an answer.
- The run has ceilings on spend and on steps, they are on by default, and hitting
  one ends the run instead of degrading quietly.
- The final status is `degraded`, not `ok`. Every step that ran succeeded, and the
  run still stopped short of its plan, so calling it `ok` would hide the ceiling.

## Honesty notes

- The hang is injected, not a real network fault: the tool blocks on a
  `threading.Event` nobody sets. What is real is the enforcement path, which
  measures wall-clock time and cuts the call off, and which cannot tell the
  difference between this and a dead socket.
- The prices are made up. The ledger arithmetic, the per-attempt charging and the
  halt are not.
- This is a portfolio harness, not a production service.
