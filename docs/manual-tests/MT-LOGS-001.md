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

**Action**:

```pwsh
$run = ./bin/persatrix run feature-builder --input '{"user_request":"Add a ping endpoint"}' --wait | ConvertFrom-Json
./bin/persatrix logs $run.execution_id --verbose | Select-Object -First 20
```

**Expected**:
- Exit code 0.
- Output contains entries from both orchestrator (`service.kind=orchestrator`) and at least one
  agent (`service.kind=agent`), all carrying matching `execution_id` and (where a span was active)
  matching `trace_id` / `span_id`.

**Verification**:
- [ ] At least one `agent` and one `orchestrator` line present
- [ ] All printed lines share the same `execution_id`

---

### Step 2: Filter behaviour

**Action**:

```pwsh
./bin/persatrix logs $run.execution_id --level WARN
./bin/persatrix logs $run.execution_id --since 5m
./bin/persatrix logs $run.execution_id --workflow feature-builder
./bin/persatrix logs _ --since 1h | Measure-Object -Line
```

**Expected**:
- `--level WARN` returns only WARN/ERROR-or-equivalent lines (exact match per server contract).
- `--since 5m` returns the same lines as Step 1 (the run completed within the window).
- `--workflow feature-builder` returns lines whose `workflow` attribute matches.
- `id=_` cross-execution view returns a non-empty merged stream.

**Verification**:
- [ ] Each filter narrows the output as expected
- [ ] `id=_` returns a chronologically merged stream

---

### Step 3: SSE `--follow` + reconnect marker

**Action** (terminal A):

```pwsh
./bin/persatrix run feature-builder --input '{"user_request":"long-running"}' | Out-Null
# capture the execution id, then in terminal B:
./bin/persatrix logs --follow <execution_id>
```

While the follow session is open, restart the orchestrator (`Ctrl-C` then `make run` again).

**Expected**:
- Terminal B prints live entries as they arrive.
- On the orchestrator restart, terminal B prints exactly one `info: [reconnected]` line, then
  resumes streaming.
- Backoff resets after the successful reconnect (a second transient drop within the same session
  starts again from the initial backoff window, not the inflated one).

**Verification**:
- [ ] Live entries observed within ~2s of being produced
- [ ] Single `[reconnected]` marker on restart
- [ ] No runaway backoff after a single reconnect

---

### Step 4: Restart durability

**Action**:

```pwsh
$run2 = ./bin/persatrix run feature-builder --input '{"user_request":"durability"}' --wait | ConvertFrom-Json
# stop the orchestrator
# rm -r data/logs/$run2.execution_id  ← do NOT delete; we want to verify warm-load
# start it again: `make run`
./bin/persatrix logs $run2.execution_id | Measure-Object -Line
```

**Expected**: Pre-restart entries are still queryable after the orchestrator process restart.

**Verification**:
- [ ] Line count matches what was visible before the restart

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
./bin/persatrix logs $run.execution_id --trace <trace_id>
```

**Expected**: Output is restricted to entries whose `trace_id` matches; entries from both
orchestrator and agent appear together (proving the cross-process correlation from RFC 0018 PR 3).

**Verification**:
- [ ] All printed lines carry the requested `trace_id`
- [ ] Orchestrator + agent lines coexist in the result

---

## Pass / Fail Criteria

- **Pass**: Steps 1–6 each meet their Verification checkboxes.
- **Accepted-with-known-gap**: `--trace` server-side filtering is currently client-side; entries are
  still shipped over the wire (tracked deferral). Operator behaviour is unchanged.
- **Fail**: Any step's Verification fails, or restart durability loses entries.
