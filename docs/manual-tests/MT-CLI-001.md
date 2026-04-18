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

**Out of Scope**: Agent LLM execution quality; `orch` subcommands that are stubs in v0.1
(`init`, `replay`, `cost`, `state`, `node`, `mesh`).

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
- [ ] Exit code 0
- [ ] Subcommand list includes at least `run`, `status`, `validate`, `agent`

---

### Step 2: Validate Config via CLI

**Action**:

```bash
./bin/orch validate config/
```

**Expected Result**: Command exits 0 with a message indicating all files are valid.

**Verification**:
- [ ] Exit code 0
- [ ] Output does not contain `error` or `invalid`

---

### Step 3: Submit a Workflow Run

**Action**:

```bash
./bin/orch run feature-builder --input '{"user_request":"Add a ping endpoint"}'
```

(If the orchestrator is not on the default `http://localhost:8080`, pass
`--server http://127.0.0.1:8080`.)

**Expected Result**: The CLI prints a run ID and initial status. The command may block until the
run completes or return immediately with a run ID depending on implementation.

**Verification**:
- [ ] Exit code 0
- [ ] Output contains a `run_id` (UUID format) or a clear status line
- [ ] No panic or unhandled Rust `unwrap` backtrace in output

---

### Step 4: Check Run Status via CLI

**Action**: Replace `<RUN_ID>` with the value printed in Step 3.

```bash
./bin/orch status <RUN_ID>
```

If Step 3 blocks until completion, use the run ID from its output. If no specific run ID is
available, `orch status` with no argument lists all runs:

```bash
./bin/orch status
```

**Expected Result**: Status output includes `run_id`, `workflow_id`, and a `status` field
(`running`, `completed`, or `failed`).

**Verification**:
- [ ] Exit code 0
- [ ] Output contains a status value from the expected set
- [ ] JSON or structured output is well-formed (no partial output or truncation)

---

### Step 5: List Agents via CLI

**Action**:

```bash
./bin/orch agent list
```

**Expected Result**: A list (possibly empty if no agents are registered) printed without error.

**Verification**:
- [ ] Exit code 0
- [ ] Output is a valid JSON array or a human-readable table
- [ ] No 500 error or panic text

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | `orch --help` prints subcommand list | ☐ |
| 2 | `orch validate config/` exits 0 | ☐ |
| 3 | `orch run` returns a run ID | ☐ |
| 4 | `orch status <id>` returns run status | ☐ |
| 5 | `orch agent list` exits 0 | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Orchestrator Not Running

**Scenario**: `orch run` is invoked before starting the orchestrator.

**Expected Behavior**: The CLI prints a connection-refused error to stderr and exits non-zero.
It must not panic.

### Edge Case 2: Unknown Workflow Name

**Scenario**: `orch run nonexistent-workflow --input '{}'`

**Expected Behavior**: CLI prints the orchestrator's error response (4xx) and exits non-zero.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| | | | | |

---

## Notes

- Build the CLI with `make all` or `make build-cli` (runs `cargo build --release` in `cli/`).
- The CLI binary path is `bin/orch` on Unix and `bin/orch.exe` on Windows.
- Stub subcommands (`init`, `replay`, `cost`, `state`, `node`, `mesh`) are intentionally not
  tested here — they are v0.3+ features.
