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
- Windows: PowerShell 7+ (commands below assume `pwsh`). Linux/macOS users substitute the
  shell-specific equivalents noted inline.
- An `ANTHROPIC_API_KEY` is **required for Scenario B** (full-agent path used by Steps 1
  and 6). Without it the workflow run fails at planner registration and the steps that
  depend on agent-side entries (`service_kind=agent`, `trace_id`) become N/A.

### One-time build

```pwsh
make build-orchestrator    # → bin/persatrix-server(.exe)
make build-cli             # → bin/persatrix(.exe)
make build-agents          # installs Python deps into the active venv
```

`make build-agents` does an editable `pip install -e ".[dev]"` inside `agents/`. **Activate
your venv first** (`./.venv/Scripts/Activate.ps1` on Windows / `source .venv/bin/activate`
on Unix); otherwise the install lands in the system Python and `make run-agent` may still
fail with `ModuleNotFoundError: No module named 'structlog'`.

### Test data

The procedure uses [`workflows/feature-builder.yaml`](../../workflows/feature-builder.yaml)
which orchestrates three agents: `planner`, `code-writer`, `code-reviewer`. The workflow may
finish with `Status: failed` (e.g. `Max LLM call iterations exceeded`) — that is **fine**
for this test. Logs are produced and queryable regardless of the final run status.

### Two execution scenarios

| Scenario | Use for | Setup |
|---------|---------|-------|
| **A — orchestrator-only smoke** | Steps 2, 3, 4, 5 (and Step 1 fallback) | Just `make run`. The submitted run fails fast at "planner not registered"; only `service_kind=orchestrator` entries are produced. |
| **B — full agents (REST + cross-process correlation)** | Steps 1 (full pass) and 6 (`--trace`) | Start orchestrator + the three agents below. Both `service_kind=orchestrator` and `service_kind=agent` entries are produced and share `trace_id`. |

### Environment Setup — Scenario B (full agents)

Run each block in its **own** terminal. The orchestrator's HTTP API listens on
`127.0.0.1:8080`; gRPC on `127.0.0.1:9090`. Agents auto-register with the orchestrator
on startup (look for `Registered agent <id> with orchestrator at http://127.0.0.1:8080`).

> **Port collision recovery (Windows / pwsh)** — run before any `make run` /
> `run-agent` if a previous session left a process behind:
>
> ```pwsh
> Get-Process persatrix-server -ErrorAction SilentlyContinue | Stop-Process -Force
> Get-NetTCPConnection -LocalPort 8080,9090,50051,50052,50053 -ErrorAction SilentlyContinue |
>   ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
> ```

**Terminal 0 — orchestrator:**

```pwsh
$env:ANTHROPIC_API_KEY = "<your key>"   # if not already in your shell profile
make run                                 # or: ./bin/persatrix-server.exe --config config/
```

Verify it is live (in any other terminal):

```pwsh
(Invoke-WebRequest http://127.0.0.1:8080/api/v1/executions/_/logs -UseBasicParsing).StatusCode
# expect: 200
```

**Terminals 1, 2, 3 — agents** (one per terminal; activate venv first):

```pwsh
./.venv/Scripts/Activate.ps1
$env:ANTHROPIC_API_KEY = "<your key>"
$env:PYTHONPATH        = "agents/generated"
python -m persatrix_agents.server --agent planner       --port 50051
# in next terminal:
python -m persatrix_agents.server --agent code-writer   --port 50052
# in next terminal:
python -m persatrix_agents.server --agent code-reviewer --port 50053
```

> The `make run-agent AGENT=… PORT=…` target uses bash-style env-prefix syntax
> (`PYTHONPATH=… python …`) which does not work in pwsh — use the explicit `python -m …`
> commands above on Windows.

> **OTLP collector warnings are harmless.** Both orchestrator and agents will print
> `dial tcp [::1]:4318: connectex: No connection could be made…` if no OTLP collector
> is running. Logs functionality is unaffected.

### Inspecting raw log JSON

The CLI (`persatrix logs`) renders human-readable text — it does **not** print the
underlying JSON. To see the raw entry shape (e.g. to confirm `service_kind`, `trace_id`,
or `attributes` values), call the REST endpoint directly:

```pwsh
$entries = Invoke-RestMethod -Uri "http://127.0.0.1:8080/api/v1/executions/$runId/logs"
$entries.Count
$entries | Where-Object { $_.service_kind -eq 'agent' } | Select-Object -First 1 |
  ConvertTo-Json -Depth 5
```

The response body is a **JSON array** of entries (not an object), and per-entry fields are
underscore-cased: `service_kind`, `service_instance`, `execution_id`, `trace_id`, `span_id`,
`attributes`. Cross-execution merge view: `…/api/v1/executions/_/logs`.

---

## Test Procedure

### Step 1: REST round-trip on a completed execution

`persatrix run` is **fire-and-forget** — it prints `OK Workflow <id> submitted (run_id:
<run_id>)` and returns immediately (there is no `--wait` flag). Capture the `run_id` from
stdout, poll `persatrix status <run_id>` until `Status` is no longer `pending`/`running`,
then query logs.

**Action** (Scenario B — full agents recommended; Scenario A also valid, see Expected):

```pwsh
$out   = ./bin/persatrix.exe run feature-builder --input '{"user_request":"Add a ping endpoint"}'
$runId = ([regex]'run_id:\s*([^)]+)').Match(($out -join "`n")).Groups[1].Value.Trim()
"runId=$runId"

do {
    Start-Sleep -Seconds 3
    $status = (./bin/persatrix.exe status $runId) -join "`n"
} while ($status -match 'Status:\s*(pending|running)')
$status

./bin/persatrix.exe logs $runId --verbose | Select-Object -First 20
```

The CLI prints two lines per entry in `--verbose` mode (header + an indented attributes
line). To bucket entries by `service_kind` and confirm the cross-process split, hit the
REST endpoint directly:

```pwsh
$entries = Invoke-RestMethod -Uri "http://127.0.0.1:8080/api/v1/executions/$runId/logs"
"Total:        $($entries.Count)"
"Orchestrator: $(($entries | Where-Object { $_.service_kind -eq 'orchestrator' }).Count)"
"Agent:        $(($entries | Where-Object { $_.service_kind -eq 'agent' }).Count)"
"With trace_id:$(($entries | Where-Object { $_.trace_id }).Count)"
```

**Expected**:
- Exit code 0 from `persatrix logs`.
- **Scenario B**: both `service_kind=orchestrator` (≈ 15) and `service_kind=agent` (≥ 1)
  entries are present; all share the same `execution_id` (= `$runId`); agent-side entries
  carry matching `trace_id`/`span_id`. (A representative live run produced 24 entries:
  15 orchestrator + 9 agent, with 9 entries sharing one `trace_id`.)
- **Scenario A** (no agents registered): only `service_kind=orchestrator` lines are
  produced and the agent-side check is N/A. The orchestrator self-ingest path is still
  validated.

**Verification**:
- [ ] `Total > 0` and `Orchestrator > 0`
- [ ] `Agent > 0` (Scenario B only — N/A under Scenario A)
- [ ] All entries share the same `execution_id` (= `$runId`)

---

### Step 2: Filter behaviour

**Action**:

```pwsh
./bin/persatrix.exe logs $runId --level WARN
./bin/persatrix.exe logs $runId --level ERROR
./bin/persatrix.exe logs $runId --since 5m
./bin/persatrix.exe logs $runId --workflow feature-builder
./bin/persatrix.exe logs _ --since 1h | Measure-Object -Line
```

**Expected**:
- `--level <X>` returns only entries whose `level` equals `<X>` exactly (case-insensitive).
  The server filter is exact-match — `--level WARN` does **not** include `ERROR`. An empty
  result is valid when the run produced no entries at that level. Representative live run
  (Scenario B): 3 lines for `WARN`, 1 line for `ERROR` (= run-failure entry).
- `--since 5m` returns the same lines as Step 1 (the run completed within the window).
- `--workflow feature-builder` returns only entries whose `attributes.workflow` field
  matches. Today **only** the orchestrator's `run created` / `executing run` entries set
  the `workflow` attribute — agent-side entries set `workflow_id` instead, which this
  filter does not match. Expect a small subset (≈ 2 CLI lines = 1 entry × 2 lines per
  entry in default mode). Tracked in
  [#179](https://github.com/mkhomutov/Persatrix/issues/179) ("propagate `workflow` onto
  every execution-scoped entry").
- `id=_` cross-execution view returns a non-empty merged stream from all recent runs.

**Verification**:
- [ ] Each filter narrows the output as expected (empty WARN result acceptable)
- [ ] `id=_` returns a chronologically merged stream

---

### Step 3: SSE `--follow` + reconnect marker

**Action** (terminal A):

```pwsh
# Terminal A — submit a run, capture its id:
$out3   = ./bin/persatrix.exe run feature-builder --input '{"user_request":"long-running"}'
$runId3 = ([regex]'run_id:\s*([^)]+)').Match(($out3 -join "`n")).Groups[1].Value.Trim()
"$runId3"

# Terminal B — follow (substitute the run_id printed in terminal A):
./bin/persatrix.exe logs --follow $runId3
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
$out2   = ./bin/persatrix.exe run feature-builder --input '{"user_request":"durability"}'
$runId2 = ([regex]'run_id:\s*([^)]+)').Match(($out2 -join "`n")).Groups[1].Value.Trim()
do {
    Start-Sleep -Seconds 3
    $status = (./bin/persatrix.exe status $runId2) -join "`n"
} while ($status -match 'Status:\s*(pending|running)')
$beforeRestart = (./bin/persatrix.exe logs $runId2 | Measure-Object -Line).Lines

# Stop the orchestrator (Ctrl-C in the make run terminal). Do NOT delete
# data/logs/$runId2 — the warm-load path is what we are checking.
# Then start it again: `make run`.

$afterRestart = (./bin/persatrix.exe logs $runId2 | Measure-Object -Line).Lines
"before=$beforeRestart after=$afterRestart"
Test-Path "data/logs/$runId2"   # expect: False (see Known gap below)
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
./bin/persatrix-server.exe --config config/    # or: make run
```

**Expected**:
- Orchestrator stdout switches from one-JSON-per-line to the zap dev console encoder
  (coloured key/value form).
- The `LogService` shipper still receives schema-conformant JSON entries (verify by running
  `./bin/persatrix.exe logs <execution_id>` from another terminal — output is unchanged).

**Verification**:
- [ ] Orchestrator stdout is human-readable
- [ ] `persatrix logs` REST output remains JSON-shaped

---

### Step 6: `--trace <trace_id>` correlation

**Action** — first pick a `trace_id` from the run. The simplest way is to query REST and
filter for non-empty `trace_id`:

```pwsh
$entries = Invoke-RestMethod -Uri "http://127.0.0.1:8080/api/v1/executions/$runId/logs"
$traceId = ($entries | Where-Object { $_.trace_id } | Select-Object -First 1).trace_id
"traceId=$traceId"

./bin/persatrix.exe logs $runId --trace $traceId --verbose
```

> **Note**: `trace_id` is populated only on entries emitted with a span-bound logger (e.g.
> inside `executor.dispatch` while an agent task is running). Most scheduler / state-store
> entries are emitted without a span context and display `trace_id=-` in `--verbose` mode.
> Under **Scenario A** (no agents registered) **no entry carries a `trace_id`** and this
> step is N/A — record it as Skipped, not Failed.

**Expected**: Output is restricted to entries whose `trace_id` matches; entries from both
orchestrator and agent appear together (proving the cross-process correlation from RFC 0018
PR 3). Representative Scenario B run produced 9 entries spanning the `planner` and
`code-writer` agents under one `trace_id`.

**Verification**:
- [ ] All printed lines carry the requested `trace_id`
- [ ] Orchestrator + agent lines coexist in the result (Scenario B)

---

## Pass / Fail Criteria

- **Pass**: Steps 1–3, 5, and 6 each meet their Verification checkboxes; Step 4 is reported as
  `Skipped (Accepted-with-known-gap)` per its inline note.
- **Accepted-with-known-gap**:
  - Step 4 (restart durability) until `Buffer.Seal` is wired into the workflow lifecycle
    (RFC 0018 PR 7 follow-up). Verified live: `data/logs/<runId>` is **not** created even
    after a clean shutdown of a completed full-agent run.
  - `--trace` server-side filtering is currently client-side; entries are still shipped over
    the wire. Tracked in [#179](https://github.com/mkhomutov/Persatrix/issues/179)
    ("CLI logs polish residuals" → "add a server-side `trace` query parameter"). Operator
    behaviour is unchanged. (Linked per PR #180 review Should-Fix #2 — prevent the documented
    gap from being orphaned.)
  - Step 1's agent-side check and Step 6 are reported as `N/A (Skipped)` when run under
    **Scenario A** (no agents registered), because no agent-side entry then exists.
- **Fail**: Any non-skipped step's Verification fails, or the warm-load code path itself
  regresses (covered by `TestWarmLoadAfterReopen`).
