# Manual Test MT-CLI-001: `orch run` End-to-End Against a Running Orchestrator

**Test ID**: `MT-CLI-001`
**Feature Area**: CLI
**Version**: 1.0
**Created**: 2026-04-18
**Last Updated**: 2026-04-18
**Status**: Active

---

## Overview

**Purpose**: Verify that the `orch` CLI can submit a workflow run to a live orchestrator and print
the result, exercising the full Rust CLI → REST → orchestrator round-trip.

**Scope**: `orch run`, `orch status`, and `orch validate` subcommands against a locally running
orchestrator.

**Out of Scope**: Agent LLM execution quality; stub subcommands not yet implemented:
- v0.2 stubs (print "not yet implemented", exit 0): `init`, `replay`, `cost`, `state`
- v0.3+ stubs (labeled in help, print "not yet implemented", exit 0): `node`, `mesh`

---

## Related Documentation

**Feature Documentation**:
- [cli/src/main.rs](../../cli/src/main.rs) — CLI command definitions
- [docs/ai-agents-orchestration-spec.md](../ai-agents-orchestration-spec.md)

**Related Automated Tests**:
- None — CLI end-to-end tests require a live server.

---

## Preconditions

### System Requirements

**Operating Systems**:
- ☐ Windows 10/11 (x64)
- ☐ macOS 12.0+ (Intel/Apple Silicon)
- ☐ Linux (Ubuntu 22.04+)

**Dependencies Installed**:
- Go 1.24+: `go version`
- Rust (stable): `rustc --version` and `cargo --version`
- `curl` available in PATH (optional, for cross-checking)

### Application State

**Orchestrator Setup**:
- ☐ Full build complete: `make all` (builds orchestrator + CLI)
- ☐ Orchestrator running in a separate terminal: `make run`
- ☐ Config valid: `make validate` exits 0
- ☐ CLI binary present: `ls bin/orch` (or `ls bin/orch.exe` on Windows)

### Test Data

- Workflow: `feature-builder` — loaded from `workflows/feature-builder.yaml` by the orchestrator
- Input: `{"user_request": "Add a ping endpoint"}`

---

## Test Procedure

### Step 1: Verify the `orch` Binary Exists

**Action**:

```bash
./bin/orch --help
```

**Expected Result**: Usage text is printed listing subcommands: `run`, `status`, `validate`,
`agent`, `logs`, `test`, and others.

**Verification**:
- [x] Exit code 0
- [x] Subcommand list includes at least `run`, `status`, `validate`, `agent`

---

### Step 2: Validate Config via CLI

**Action**:

```bash
./bin/orch validate config/
```

**Expected Result**: Command exits 0 with a message indicating all files are valid.

**Verification**:
- [x] Exit code 0
- [x] Output does not contain `error` or `invalid`

---

### Step 3: Submit a Workflow Run

**Action**:

```bash
./bin/orch run feature-builder --input '{"user_request":"Add a ping endpoint"}'
```

(If the orchestrator is not on the default `http://localhost:8080`, pass
`--server http://127.0.0.1:8080`.)

**Expected Result**: The command is non-blocking: it submits the run and returns immediately with
the run ID and initial status:

```
OK Workflow feature-builder submitted (run_id: <uuid>)
  Status: pending
```
**Verification**:
- [x] Exit code 0
- [x] Output contains `run_id:` followed by a UUID
- [x] `Status:` line shows `pending` (or `running`)
- [x] No panic or unhandled Rust `unwrap` backtrace in output

---

### Step 4: Check Run Status via CLI

**Action**: Replace `<RUN_ID>` with the value printed in Step 3.

```bash
./bin/orch status <RUN_ID>
```

If no specific run ID is available, `orch status` with no argument lists all runs:

```bash
./bin/orch status
```

**Expected Result**: Human-readable status output includes `Run ID`, `Workflow`, and `Status`
fields (`running`, `completed`, or `failed`).

**Verification**:
- [x] Exit code 0
- [x] Output contains a status value from the expected set
- [x] JSON or structured output is well-formed (no partial output or truncation)

---

### Step 5: List Agents via CLI

**Action**:

```bash
./bin/orch agent list
```

**Expected Result**: Exit 0 with either:
- `No agents registered.` — when no agents are connected, or
- A formatted table of agents — when agents are registered.

Output is always human-readable text, never raw JSON.

**Verification**:
- [x] Exit code 0
- [x] Output is `No agents registered.` (empty case) or a formatted table (populated case)
- [x] No 500 error or panic text

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | `orch --help` prints subcommand list | ☑ |
| 2 | `orch validate config/` exits 0 | ☑ |
| 3 | `orch run` returns a run ID | ☑ |
| 4 | `orch status <id>` returns run status | ☑ |
| 5 | `orch agent list` exits 0 | ☑ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Orchestrator Not Running

**Scenario**: `orch run` is invoked before starting the orchestrator.

**Expected Behavior**: The CLI prints a connection-refused error to stderr and exits non-zero.
It must not panic.

### Edge Case 2: Unknown Workflow Name

**Scenario**: `orch run nonexistent-workflow --input '{}'`

**Expected Behavior**: CLI prints the orchestrator's error response (4xx) and exits non-zero.

### Edge Case 3: Missing `<WORKFLOW>` Argument

**Scenario**: `orch run` invoked with no arguments.

**Expected Behavior**: clap prints a usage error to stderr and exits non-zero:
```
error: the following required arguments were not provided:
  <WORKFLOW>

Usage: orch.exe run <WORKFLOW>
```

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-04-18 | Copilot | Windows 11 | Pass | All 5 steps passed. `--help` exit 0 with full subcommand list. `validate config/` → "Validation passed (3 file(s) checked)". `run feature-builder` → HTTP 201, `run_id=a1badf22`, status `pending`. `status <id>` → structured output showing `failed` (no agents registered, expected). `agent list` → "No agents registered.", exit 0. |
| 2026-04-18 | Copilot | Windows 11 | Pass | Re-verified Steps 1-5 and Edge Cases 1-3 against live orchestrator. `run feature-builder` output now prints ASCII-safe `OK Workflow ...` marker (fixed Windows mojibake issue from Unicode checkmark). Latest run: `run_id=7d846bcc-bc3d-4590-9ef6-e7e84c0acd4b`, `Status: pending`; `status` then shows terminal `failed` due no `planner` agent registered (expected in this setup). |
| 2026-04-18 | mkhomutov | Windows 11 | Pass | Retest — all 5 steps pass. `run_id=4d418cb4`, `Status: pending`, terminal `failed` (no `planner` registered). `agent list` shows `sarah-chen` healthy on port 50055 (lingering from prior session). |

---

## Notes

- Build the CLI with `make all` or `make build-cli` (runs `cargo build --release` in `cli/`).
- The CLI binary path is `bin/orch` on Unix and `bin/orch.exe` on Windows.
- Stub subcommands are intentionally not tested here:
  - `init`, `replay`, `cost`, `state` — v0.2 stubs: accept arguments, print
    `Command 'X' not yet implemented` (yellow), and exit 0.
  - `node`, `mesh` — v0.3+ stubs (labeled as such in `--help`): same behavior.
