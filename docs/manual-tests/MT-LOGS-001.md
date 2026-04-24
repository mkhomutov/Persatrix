# Manual Test MT-LOGS-001: `persatrix logs` end-to-end (REST + SSE follow + restart durability + pretty mode)

**Test ID**: `MT-LOGS-001`
**Feature Area**: Observability (logs)
**Version**: 1.0
**Created**: 2026-04-24
**Last Updated**: 2026-04-24
**Status**: Active

---

## Overview

**Purpose**: Verify the operator-visible v0.2.3 logging surface end-to-end:

1. `persatrix logs <execution_id>` returns the orchestrator's ring-buffered + disk-persisted entries
   for a completed execution (REST round-trip).
2. `persatrix logs --follow <execution_id>` streams live entries over SSE for a long-running
   execution and prints a single `[reconnected]` info line on transient stream loss.
3. Entries survive an orchestrator restart (warm-load from `data/logs/<execution_id>/<seq>.jsonl`).
   **Currently Accepted-with-known-gap** — see Step 4 for details; the warm-load code path is
   exercised by unit tests until seal-on-completion is wired in (RFC 0018 PR 7 follow-up).
4. `PERSATRIX_LOG_FORMAT=pretty` selects the developer console encoder for the orchestrator process
   without disturbing the schema-conformant JSON output of the `LogService` shipper.

**Scope**: `persatrix logs`, `persatrix logs --follow`, `persatrix logs --since/--workflow/--level`,
`persatrix logs --trace <id>`, `PERSATRIX_LOG_FORMAT=pretty` env override.

**Out of Scope**: Full Collector pipeline (covered by RFC 0019 manual tests); auth on the `logs`
endpoint (deferred to RFC 0009).

---

## Related Documentation

- [docs/observability.md](../observability.md) §11 (Logs operations + CLI usage)
- [docs/rfcs/0018-structured-logging-framework.md](../rfcs/0018-structured-logging-framework.md)
- [cli/src/commands/logs.rs](../../cli/src/commands/logs.rs)
- [internal/server/logs_handler.go](../../internal/server/logs_handler.go)
- [internal/observability/logbuffer/](../../internal/observability/logbuffer)

**Related Automated Tests**:
- `internal/server/logs_handler_test.go`, `logs_stream_handler_test.go`, `logs_service_test.go`
- `agents/tests/test_log_shipper.py`
- `tests/integration/test_logs_e2e.py`

---

## Preconditions

### System Requirements

- Go 1.24+, Rust stable, Python 3.11+
- `make all` completes cleanly
- Orchestrator running locally: `make run`

### Test Data

- A workflow that produces ≥ 5 log lines across orchestrator + agent (e.g. `feature-builder` from
  `workflows/feature-builder.yaml`).

---

## Test Procedure

### Step 1: REST round-trip on a completed execution

`persatrix run` prints a human-readable line of the form
`OK Workflow <id> submitted (run_id: <run_id>)` and returns immediately — there
is no `--wait` flag. Capture the `run_id` from stdout, then poll
`persatrix status <run_id>` until `Status` is no longer `pending`/`running`
before querying logs.

**Action**:

```pwsh
$out = ./bin/persatrix run feature-builder --input '{"user_request":"Add a ping endpoint"}'
$runId = ([regex]'run_id:\s*([^)]+)').Match(($out -join "`n")).Groups[1].Value.Trim()
do {
    Start-Sleep -Seconds 1
    $status = (./bin/persatrix status $runId) -join "`n"
} while ($status -match 'Status:\s*(pending|running)')
./bin/persatrix logs $runId --verbose | Select-Object -First 20
```

**Expected**:
- Exit code 0.
- Output contains entries from both orchestrator (`service.kind=orchestrator`) and at least one
  agent (`service.kind=agent`), all carrying matching `execution_id` and (where a span was active)
  matching `trace_id` / `span_id`.

> **Note**: in environments without agent credentials (e.g. `ANTHROPIC_API_KEY` unset)
> the run will fail at planner registration; in that case only `service.kind=orchestrator`
> lines are produced, which still validates the orchestrator self-ingest path. The
> agent-side check then requires a fully provisioned environment.

**Verification**:
- [ ] At least one `orchestrator` line present (and one `agent` line when credentials available)
- [ ] All printed lines share the same `execution_id` (matches `$runId`)

---

### Step 2: Filter behaviour

**Action**:

```pwsh
./bin/persatrix logs $runId --level WARN
./bin/persatrix logs $runId --since 5m
./bin/persatrix logs $runId --workflow feature-builder
./bin/persatrix logs _ --since 1h | Measure-Object -Line
```

**Expected**:
- `--level WARN` returns only entries whose `level` field equals `WARN` exactly
  (case-insensitive). The server filter is exact-match per
  [`internal/server/logs_handler.go`](../../internal/server/logs_handler.go) `levelMatch` —
  `--level WARN` does **not** include `ERROR`, and an empty result is valid when
  the run produced no WARN-tagged entries.
- `--since 5m` returns the same lines as Step 1 (the run completed within the window).
- `--workflow feature-builder` returns lines whose `workflow` attribute matches.
  Today only the `run created` / `executing run` entries carry the `workflow`
  attribute, so the filter typically returns a small subset (not the full per-run
  log) — see [#179](https://github.com/mkhomutov/Persatrix/issues/179) for the
  follow-up to propagate `workflow` onto every execution-scoped entry.
- `id=_` cross-execution view returns a non-empty merged stream.

**Verification**:
- [ ] Each filter narrows the output as expected (empty WARN result acceptable)
- [ ] `id=_` returns a chronologically merged stream

---

### Step 3: SSE `--follow` + reconnect marker

**Action** (terminal A):

```pwsh
$out3 = ./bin/persatrix run feature-builder --input '{"user_request":"long-running"}'
$runId3 = ([regex]'run_id:\s*([^)]+)').Match(($out3 -join "`n")).Groups[1].Value.Trim()
"$runId3"
# then in terminal B (substitute the run_id printed in terminal A):
./bin/persatrix logs --follow $runId3
```

While the follow session is open, restart the orchestrator (`Ctrl-C` then `make run` again).

> **Note**: `persatrix run` is fire-and-forget — it submits and returns immediately; the
> `--input` payload does **not** influence run duration. "Live entries" only appear in
> Terminal B if execution is still in progress when `--follow` connects. In an environment
> without registered agents the run completes in milliseconds and no new entries arrive
> after `--follow` attaches; the reconnect-marker check below is still meaningful in that
> case.

**Expected**:
- Terminal B prints live entries as they arrive (when an execution is still running).
- On the orchestrator restart, terminal B prints a transient `warning: stream read failed: …
  (reconnecting in 500ms)` line followed by exactly one `info: [reconnected]` line, then
  resumes streaming.
- Backoff resets after the successful reconnect (a second transient drop within the same session
  starts again from the initial backoff window, not the inflated one).

**Verification**:
- [ ] Live entries observed within ~2s of being produced (skip when run already completed)
- [ ] Single `[reconnected]` marker on restart (one preceding `warning:` line is expected)
- [ ] No runaway backoff after a single reconnect

---

### Step 4: Restart durability

**Action**:

```pwsh
$out2 = ./bin/persatrix run feature-builder --input '{"user_request":"durability"}'
$runId2 = ([regex]'run_id:\s*([^)]+)').Match(($out2 -join "`n")).Groups[1].Value.Trim()
do {
    Start-Sleep -Seconds 1
    $status = (./bin/persatrix status $runId2) -join "`n"
} while ($status -match 'Status:\s*(pending|running)')
$beforeRestart = (./bin/persatrix logs $runId2 | Measure-Object -Line).Lines
# stop the orchestrator (Ctrl-C in the make run terminal).
# Do NOT delete data/logs/$runId2 — we want to verify warm-load.
# Start it again: `make run`
$afterRestart = (./bin/persatrix logs $runId2 | Measure-Object -Line).Lines
"before=$beforeRestart after=$afterRestart"
```

**Expected**: Pre-restart entries are still queryable after the orchestrator process restart
— **but only when those entries have actually been flushed to disk.**

> **Known gap (RFC 0018 PR 7 follow-up)**: Production code never invokes `Buffer.Seal()`,
> `Buffer.Close()` does not flush in-memory rings on shutdown, and `Buffer.evictLocked`
> drops un-sealed rings without flushing them. The on-disk `data/logs/<id>/` tree is
> therefore populated **only** by the `Buffer.Seal(executionID)` code path, which today
> is exercised exclusively from unit tests. A small completed run reports `after=0` after
> a clean restart — `data/logs/<runId2>` will not even exist on disk — and forcing LRU
> eviction (e.g. `PERSATRIX_LOGBUFFER_MAX_EXEC=1` plus a second run) does **not** change
> that, because the evicted ring is dropped rather than sealed.
>
> Until seal-on-completion is wired into the scheduler's terminal-state path (RFC 0018
> PR 7 follow-up — "wire `Buffer.Seal` into the workflow lifecycle", captured in
> [docs/rfcs/0018-pr-plan.md](../rfcs/0018-pr-plan.md)), Step 4 is **Accepted-with-known-gap**:
> mark it skipped and record `after=0` as the documented current behaviour. The warm-load
> code path itself is covered by `TestWarmLoadAfterReopen` in
> [`internal/observability/logbuffer/buffer_test.go`](../../internal/observability/logbuffer/buffer_test.go),
> which seeds disk via `b.Seal()` directly.

**Verification**:
- [ ] `before=$beforeRestart` recorded; `after=0` is the documented current behaviour
- [ ] Step result reported as `Skipped (Accepted-with-known-gap)` until PR 7 lands

---

### Step 5: `PERSATRIX_LOG_FORMAT=pretty` toggle

**Action** (in a fresh terminal):

```pwsh
$env:PERSATRIX_LOG_FORMAT = "pretty"
./bin/persatrix-server  # or `make run`
```

**Expected**:
- Orchestrator stdout switches from one-JSON-per-line to the zap dev console encoder
  (coloured key/value form).
- The `LogService` shipper still receives schema-conformant JSON entries (verify by running
  `./bin/persatrix logs <execution_id>` from another terminal — output is unchanged).

**Verification**:
- [ ] Orchestrator stdout is human-readable
- [ ] `persatrix logs` REST output remains JSON-shaped

---

### Step 6: `--trace <trace_id>` correlation

**Action**: pick any line from Step 1 with a non-empty `trace_id`, then:

```pwsh
./bin/persatrix logs $runId --trace <trace_id>
```

> **Note**: `trace_id` is populated only on log entries emitted with a logger derived via
> `zapenc.LoggerWithContext(ctx, logger)` (e.g. inside `executor.dispatch` while an agent
> task is running). Most scheduler / state-store entries are emitted without a span-bound
> context and therefore have `trace_id=-`. In the no-agent fallback environment described
> in Step 1 (planner not registered), **no entry carries a `trace_id`** and this step is
> N/A — record it as skipped, not failed.

**Expected**: Output is restricted to entries whose `trace_id` matches; entries from both
orchestrator and agent appear together (proving the cross-process correlation from RFC 0018 PR 3).

**Verification**:
- [ ] All printed lines carry the requested `trace_id`
- [ ] Orchestrator + agent lines coexist in the result

---

## Pass / Fail Criteria

- **Pass**: Steps 1–3, 5, and 6 each meet their Verification checkboxes; Step 4 is reported as
  `Skipped (Accepted-with-known-gap)` per its inline note.
- **Accepted-with-known-gap**:
  - Step 4 (restart durability) until `Buffer.Seal` is wired into the workflow lifecycle
    (RFC 0018 PR 7 follow-up).
  - `--trace` server-side filtering is currently client-side; entries are still shipped over
    the wire. Tracked in [#179](https://github.com/mkhomutov/Persatrix/issues/179)
    ("CLI logs polish residuals" → "add a server-side `trace` query parameter"). Operator
    behaviour is unchanged. (Linked per PR #180 review Should-Fix #2 — prevent the documented
    gap from being orphaned.)
  - Step 6 reported as `N/A (Skipped)` when run in the no-agent fallback environment
    described in Step 1, because no entry then carries a `trace_id`.
- **Fail**: Any non-skipped step's Verification fails, or the warm-load code path itself
  regresses (covered by `TestWarmLoadAfterReopen`).
