# Manual Test MT-CONFIG-001: `make validate` Catches Malformed `config/agents.yaml`

**Test ID**: `MT-CONFIG-001`
**Feature Area**: Config
**Version**: 1.0
**Created**: 2026-04-18
**Last Updated**: 2026-04-18
**Status**: Active

---

## Overview

**Purpose**: Verify that `make validate` (which runs `python3 agents/validate.py config/`) detects
schema violations in `config/agents.yaml` and exits non-zero with a human-readable error message.

**Scope**: `agents/validate.py` schema validation for `config/agents.yaml` against
`schemas/agent.schema.json`.

**Out of Scope**: Workflow YAML validation; runtime enforcement of config values; orchestrator
startup behavior.

---

## Related Documentation

**Feature Documentation**:
- [agents/validate.py](../../agents/validate.py) — validation script
- [schemas/agent.schema.json](../../schemas/agent.schema.json) — agent schema (Draft-7)

**Related Automated Tests**:
- Unit tests: `tests/unit/python/` (schema validation helpers)

---

## Preconditions

### System Requirements

**Operating Systems**:
- ☐ Windows 10/11 (x64)
- ☐ macOS 12.0+ (Intel/Apple Silicon)
- ☐ Linux (Ubuntu 22.04+)

**Dependencies Installed**:
- Python 3.11+: `python3 --version`
- Python dev dependencies installed: `make build-agents` (installs `jsonschema`)

### Application State

- ☐ `make validate` currently exits 0 on the unmodified repo (baseline green)

### Test Data

All mutations are applied to `config/agents.yaml` in place. **The file must be restored to its
original contents at the end of this test** — see Teardown.

---

## Test Procedure

### Step 1: Confirm Baseline is Green

**Action**:

```bash
make validate
echo "Exit code: $?"
```

**Expected Result**: Exit code 0; no error output.

**Verification**:
- [ ] Exit code 0
- [ ] No validation errors printed

---

### Step 2: Remove a Required Field (`name`)

**Action**: Edit `config/agents.yaml`. For the first agent entry, remove the `name:` line. Save
the file.

```bash
# Save original for teardown
cp config/agents.yaml config/agents.yaml.bak

# Open in editor and delete the 'name:' line from the first agent
# Windows: notepad config\agents.yaml  |  macOS/Linux: nano config/agents.yaml
```

Then run:

```bash
make validate
echo "Exit code: $?"
```

**Expected Result**: Exit code 1 (non-zero). Output includes a message referencing
`config/agents.yaml`, the affected agent, and the missing `name` property.

**Verification**:
- [ ] Exit code is non-zero
- [ ] Validation output names `config/agents.yaml`
- [ ] Error message mentions `name` or `required` property
- [ ] No Python traceback (clean error, not a crash)

---

### Step 3: Set an Invalid Agent ID

**Action**: Restore the file, then change the `id` of the first agent to an invalid value (e.g.,
`"My Agent!"` — contains spaces and `!`, violating the regex
`^[a-z0-9][a-z0-9-]*[a-z0-9]$`).

```bash
cp config/agents.yaml.bak config/agents.yaml
# Edit: change first agent id to "My Agent!"
# Windows: notepad config\agents.yaml  |  macOS/Linux: nano config/agents.yaml

make validate
echo "Exit code: $?"
```

**Expected Result**: Exit code 1. Error message references the `id` field pattern violation.

**Verification**:
- [ ] Exit code is non-zero
- [ ] Error message mentions `id` or `pattern`
- [ ] No Python traceback

---

### Step 4: Set an Invalid `type` Value

**Action**: Restore the file, then change the `type` of the first agent to `"unknown"` (not in
the allowed enum `["task", "persona"]`).

```bash
cp config/agents.yaml.bak config/agents.yaml
# Edit: change first agent type to "unknown"
# Windows: notepad config\agents.yaml  |  macOS/Linux: nano config/agents.yaml

make validate
echo "Exit code: $?"
```

**Expected Result**: Exit code 1. Error message references the `type` field enum violation.

**Verification**:
- [ ] Exit code is non-zero
- [ ] Error message mentions `type` or `enum`
- [ ] No Python traceback

---

### Step 5: Introduce a YAML Syntax Error

**Action**: Restore the file, then introduce a YAML syntax error (e.g., unmatched indent or
missing colon).

```bash
cp config/agents.yaml.bak config/agents.yaml
# Edit: add a line like "  bad: indent: here" in the middle of an agent block
# Windows: notepad config\agents.yaml  |  macOS/Linux: nano config/agents.yaml

make validate
echo "Exit code: $?"
```

**Expected Result**: Exit code 1. Error output mentions YAML parse error (not a schema error).

**Verification**:
- [ ] Exit code is non-zero
- [ ] Error output references the file and an invalid YAML construct
- [ ] No unhandled Python exception / traceback visible to the tester

---

### Teardown: Restore Original Config

**Action**: Restore the original config file regardless of test outcome.

```bash
cp config/agents.yaml.bak config/agents.yaml
rm config/agents.yaml.bak
make validate
echo "Exit code: $?"
```

**Verification**:
- [ ] Exit code 0 after restore
- [ ] No backup file remains in `config/`

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Baseline `make validate` exits 0 | ☐ |
| 2 | Missing `name` field → non-zero exit + error mentioning `name` | ☐ |
| 3 | Invalid agent `id` → non-zero exit + error mentioning `id` | ☐ |
| 4 | Invalid `type` value → non-zero exit + error mentioning `type` | ☐ |
| 5 | YAML syntax error → non-zero exit + parse error | ☐ |
| Teardown | Config restored, `make validate` returns to 0 | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Multiple Violations

**Scenario**: Two agents both have invalid IDs.

**Expected Behavior**: `agents/validate.py` reports all violations, not just the first one.
The exit code is still non-zero.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| | | | | |

---

## Notes

- Always run the teardown step. Leaving `config/agents.yaml` in a broken state will cause
  subsequent tests to fail at their own precondition checks.
- `make validate` is defined in `Makefile` as `python3 agents/validate.py config/`.
  Running the Python script directly produces the same result.
- The agent schema is `schemas/agent.schema.json`. Required fields per the schema:
  `id`, `name`, `role`, `model`.
