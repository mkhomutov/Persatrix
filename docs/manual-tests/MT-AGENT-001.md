# Manual Test MT-AGENT-001: Task Agent Executes a Builtin Tool (No LLM Required)

**Test ID**: `MT-AGENT-001`
**Feature Area**: Agent
**Version**: 1.0
**Created**: 2026-04-18
**Last Updated**: 2026-04-18
**Status**: Active

---

## Overview

**Purpose**: Verify that the task agent's tool-execution pipeline works end-to-end — tool
registration, permission checks, invocation, and result propagation — using the existing
integration test that stubs the LLM so no API key is required.

**Scope**: `agents/tools/builtin.py` (`file_write`), tool call dispatch in `agents/task_agent.py`,
and `AgentServiceServicer` in `agents/server.py`.

**Out of Scope**: Real LLM API calls; network/HTTP tools; memory tools; persona agents.

---

## Related Documentation

**Feature Documentation**:
- [agents/tools/builtin.py](../../agents/tools/builtin.py) — builtin tool implementations
- [agents/task_agent.py](../../agents/task_agent.py) — task agent implementation

**Related Automated Tests**:
- `tests/integration/test_agent_server.py::TestEndToEndExecution::test_task_with_tool_use`

---

## Preconditions

### System Requirements

**Operating Systems**:
- ☐ Windows 10/11 (x64)
- ☐ macOS 12.0+ (Intel/Apple Silicon)
- ☐ Linux (Ubuntu 22.04+)

**Dependencies Installed**:
- Python 3.11+: `python3 --version`
- Python dev dependencies installed: `make build-agents`
  - Required packages: `grpcio`, `grpcio-tools`, `pytest`, `pytest-asyncio`

### Application State

- ☐ No running orchestrator or agent processes required (test is fully in-process)
- ☐ No LLM API key required (`ANTHROPIC_API_KEY` may be unset)
- ☐ Proto stubs generated: `make proto` (or `agents/generated/` already present)

### Test Data

The integration test at
`tests/integration/test_agent_server.py::TestEndToEndExecution::test_task_with_tool_use`
self-contains all fixtures. No external files needed.

---

## Test Procedure

### Step 1: Confirm the Test Suite is Importable

**Action**:

```bash
python3 -c "from agents.tools.builtin import file_write; print('OK')"
```

Run from the repo root. If this fails, run `make build-agents` first.

**Expected Result**: Prints `OK` with exit code 0.

**Verification**:
- [ ] Exit code 0
- [ ] No `ModuleNotFoundError` or import error

---

### Step 2: Run the Tool-Use Integration Test

**Action**:

```bash
python3 -m pytest tests/integration/test_agent_server.py::TestEndToEndExecution::test_task_with_tool_use \
  -v --tb=short -c agents/pyproject.toml
```

**Expected Result**: The test passes. Output includes `PASSED`.

Example expected output:
```
tests/integration/test_agent_server.py::TestEndToEndExecution::test_task_with_tool_use PASSED
1 passed in <Xs>
```

**Verification**:
- [ ] Exit code 0
- [ ] Test status is `PASSED`
- [ ] No `AssertionError` or unexpected exception in the output

---

### Step 3: Inspect What the Test Verifies

The test `test_task_with_tool_use` (in
`tests/integration/test_agent_server.py`, lines ~260–310) does the following:

1. Registers a mock `file_write` tool.
2. Creates a mock LLM that returns a `TOOL_USE` stop reason with a `file_write` call
   on the first invocation, then `END_TURN` on the second.
3. Starts an in-process gRPC server with a `TaskAgent`.
4. Sends a `TaskRequest` and asserts:
   - `status == COMPLETED`
   - `metadata["tool_calls"] == "1"`

**Action**: Read the test file to confirm the assertions are as described above:

```bash
python3 -m pytest tests/integration/test_agent_server.py::TestEndToEndExecution::test_task_with_tool_use \
  -v --tb=long --capture=no -c agents/pyproject.toml
```

**Expected Result**: Test passes and the captured output includes no unexpected failures.

**Verification**:
- [ ] `status == COMPLETED` assertion passes
- [ ] `metadata["tool_calls"] == "1"` assertion passes

---

### Step 4: Run the Full `TestEndToEndExecution` Suite

**Action**: Run all tests in the class to confirm no regressions across all execution paths:

```bash
python3 -m pytest tests/integration/test_agent_server.py::TestEndToEndExecution \
  -v --tb=short -c agents/pyproject.toml
```

**Expected Result**: All tests in `TestEndToEndExecution` pass.

**Verification**:
- [ ] Exit code 0
- [ ] All tests listed as `PASSED`
- [ ] Test count matches the number of methods in `TestEndToEndExecution` (currently 6)

---

### Step 5: Confirm Tool Permission Enforcement

**Action**: Run the full integration file including `TestEmptyModelGuard`:

```bash
python3 -m pytest tests/integration/test_agent_server.py -v --tb=short -c agents/pyproject.toml
```

**Expected Result**: All tests pass, including `TestEmptyModelGuard::test_empty_model_raises_system_exit`.

**Verification**:
- [ ] Exit code 0
- [ ] All tests listed as `PASSED`

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | `agents.tools.builtin` imports cleanly | ☐ |
| 2 | `test_task_with_tool_use` PASSED | ☐ |
| 3 | Tool-use assertions (`COMPLETED`, `tool_calls==1`) pass | ☐ |
| 4 | All `TestEndToEndExecution` tests PASSED | ☐ |
| 5 | Full `test_agent_server.py` suite PASSED | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Proto Stubs Missing

**Scenario**: `agents/generated/` is absent (proto stubs not generated).

**Expected Behavior**: `ImportError` on `from agents.generated import task_pb2`. Fix by running
`make proto` and re-running the test.

### Edge Case 2: `asyncio_mode` Not Set

**Scenario**: Running pytest without the `agents/pyproject.toml` config (`-c` flag omitted).

**Expected Behavior**: Async tests may fail with `PytestUnraisableExceptionWarning` or
`RuntimeError: coroutine was never awaited`. Always pass `-c agents/pyproject.toml` to pick up
`asyncio_mode = "auto"`.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| | | | | |

---

## Notes

- This test uses a mock LLM (`AsyncMock`) — no API key or internet connection is needed.
- The builtin `file_write` tool implementation under test is in
  `agents/tools/builtin.py`. In the integration test, a lightweight mock replaces the real
  filesystem write; permission enforcement is still exercised through the tool registry.
- To run all Python tests at once (unit + integration): `make test-python`.
