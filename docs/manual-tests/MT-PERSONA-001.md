# Manual Test MT-PERSONA-001: Start Semi-Autonomous Persona; Verify Tick Loop and Logged Actions

**Test ID**: `MT-PERSONA-001`
**Feature Area**: Persona
**Version**: 1.0
**Created**: 2026-04-18
**Last Updated**: 2026-04-18
**Status**: Active

---

## Overview

**Purpose**: Verify that a semi-autonomous persona agent starts correctly, the tick scheduler
initialises with the configured interval, and ticks fire and are logged without crashing.

**Scope**: Tick scheduler startup, tick-loop log output, idle detection, graceful shutdown.

**Out of Scope**: LLM response correctness; event handling; memory persistence.

---

## Related Documentation

**Feature Documentation**:
- [docs/rfcs/0005-persona-agent-memory.md](../rfcs/0005-persona-agent-memory.md)
- [agents/tick.py](../../agents/tick.py) — `TickScheduler`
- [agents/persona_runtime/](../../agents/persona_runtime/) — `_LLMPersonaAgent`

**Related Automated Tests**:
- Unit tests: `tests/unit/python/test_agents.py`

---

## Preconditions

### System Requirements

**Operating Systems**:
- ☐ Windows 10/11 (x64)
- ☐ macOS 12.0+ (Intel/Apple Silicon)
- ☐ Linux (Ubuntu 22.04+)

**Dependencies Installed**:
- Python 3.11+: `python3 --version`
- `ANTHROPIC_API_KEY` set in environment (persona ticks call the LLM)
- Agents package installed: `make build-agents`

### Application State

- ☐ Orchestrator running: `make run` (binds to `127.0.0.1:8080`)
- ☐ Config valid: `make validate` exits 0

### Test Data

Create the log directory before starting the agent (works on all platforms):

```bash
mkdir -p logs
```

Temporarily reduce the tick interval in `config/agents.yaml` for the `sarah-chen` persona to speed
up the test:

```yaml
autonomy:
  tick_interval_seconds: 5   # test value; restore to 60 after testing
```

---

## Test Procedure

### Step 1: Start the Persona Agent

**Action**: In a dedicated terminal, start the `sarah-chen` persona agent and capture its output:

```bash
make run-agent AGENT=sarah-chen 2>&1 | tee logs/persona-001.log
```

**Expected Result**: Agent starts without errors; gRPC server binds successfully.

**Verification**:
- [ ] No Python traceback in the first 5 seconds of output
- [ ] Log line contains `"Tick scheduler started for sarah-chen"`

---

### Step 2: Confirm Tick Scheduler Startup Log

**Action**: Inspect the captured log for the scheduler startup message:

```bash
grep "Tick scheduler started" logs/persona-001.log
```

**Expected Result**: Exactly one line matching:
```
Tick scheduler started for sarah-chen (interval=5s, idle_after=10)
```

**Verification**:
- [ ] Line is present
- [ ] `interval` matches the value set in preconditions
- [ ] `idle_after` matches `autonomy.idle_after_ticks` from config (default 10)

---

### Step 3: Wait for the First Tick to Fire

**Action**: Wait 10 seconds, then check that at least one tick has been attempted:

```bash
sleep 10
grep -c "tick" logs/persona-001.log
```

**Expected Result**: Count is ≥ 1; the LLM tick loop has fired at least once.

**Verification**:
- [ ] No `"Tick error for sarah-chen"` error lines
- [ ] No Python exception traceback

---

### Step 4: Observe Idle Behaviour After Consecutive DO_NOTHING Ticks

**Action**: Wait a further 60 seconds (≥ 10 tick intervals with the test interval of 5 s), then
check for idle skip messages:

```bash
sleep 60
grep "idle" logs/persona-001.log | tail -5
```

**Expected Result**: After 10 consecutive `DO_NOTHING` ticks the scheduler logs:
```
Agent sarah-chen idle (10 ticks), skipping LLM tick
```

**Verification**:
- [ ] Idle-skip log line is present (or LLM responded with non-idle actions, which is also acceptable)
- [ ] Agent process is still running (`ps` / Task Manager confirm)

---

### Step 5: Graceful Shutdown

**Action**: Send SIGINT to the agent process (Ctrl-C in its terminal).

**Expected Result**: Scheduler stops cleanly and logs shutdown.

**Verification**:
- [ ] Log line contains `"Tick scheduler stopped for sarah-chen"`
- [ ] Process exits with code 0 or 130 (SIGINT); no unhandled exception

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Agent starts; no early traceback | ☐ |
| 2 | Tick scheduler startup log present with correct interval | ☐ |
| 3 | At least one tick fired; no tick errors | ☐ |
| 4 | Idle-skip log appears after 10 idle ticks (or LLM stays active) | ☐ |
| 5 | Graceful shutdown; scheduler-stopped log present | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: `ANTHROPIC_API_KEY` Not Set

**Scenario**: Environment variable absent when agent starts.

**Expected Behavior**: LLM client raises a configuration error on the first tick; log shows
`"Tick error for sarah-chen"` with an authentication exception. Agent does not crash the process
— subsequent ticks are attempted (and will also fail until the key is provided).

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| | | | | |

---

## Notes

- Restore `tick_interval_seconds: 60` in `config/agents.yaml` after completing this test.
- Remove the log file after testing: `rm logs/persona-001.log` (the file is `.gitignore`d via
  `logs/`; verify with `git check-ignore logs/persona-001.log` if unsure).
- A single `"Tick error"` during startup (before the LLM client initialises) is acceptable;
  repeated errors on every tick indicate a configuration problem.
