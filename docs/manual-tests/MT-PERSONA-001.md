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

Temporarily reduce the tick interval in `config/agents.yaml` for the `ember-owl` persona to speed
up the test:

```yaml
autonomy:
  tick_interval_seconds: 5   # test value; restore to 60 after testing
```

---

## Test Procedure

### Step 1: Start the Persona Agent

**Action**: In a dedicated terminal, start the `ember-owl` persona agent and capture its output:

```bash
make run-agent AGENT=ember-owl 2>&1 | tee logs/persona-001.log
```

> **Fix (2026-04-18, resolved)**: `make run-agent` previously failed with
> `ModuleNotFoundError: No module named 'task_pb2'` without a manual `PYTHONPATH=agents/generated`
> prefix. Fixed in the Makefile — `PYTHONPATH` is now set automatically by the `run-agent` target.
>
> If port 50051 (the default agent gRPC port) is already in use by another process,
> the agent prints `Failed to bind gRPC server to 127.0.0.1:50051`. Use the direct invocation
> with an explicit port instead:
> `python3 -m persatrix_agents.server --agent ember-owl --port 50055`

**Expected Result**: Agent starts without errors; gRPC server binds successfully.

**Verification**:
- [x] No Python traceback in the first 5 seconds of output
- [x] Log line contains `"Tick scheduler started for ember-owl"`

> **Run 2026-04-18 (full pass)**: Agent started cleanly on port 50057. Log output within 2 s:
> `Agent server listening on 127.0.0.1:50057`, `Serving 1 agent(s): ['ember-owl']`,
> `FTS5 enabled for episodic memory`, `Started tick scheduler for ember-owl (interval=5s)`.
> No Python traceback. One `WARNING` for Notes FTS5 fallback (non-fatal; see Step 2 notes).

---

### Step 2: Confirm Tick Scheduler Startup Log

**Action**: Inspect the captured log for the scheduler startup message:

```bash
grep "Tick scheduler started" logs/persona-001.log
```

**Expected Result**: Exactly one line matching:
```
Tick scheduler started for ember-owl (interval=5s, idle_after=10)
```

**Verification**:
- [x] Line is present
- [x] `interval` matches the value set in preconditions
- [x] `idle_after` matches `autonomy.idle_after_ticks` from config (default 10)

> **Run 2026-04-18 (full pass)**: Exact line observed:
> `Tick scheduler started for ember-owl (interval=5s, idle_after=10)`.
> Interval matches test value (5 s); idle_after=10 matches config default.
> Also observed — non-fatal warning: `Notes FTS5 query failed ... no such column: tick, falling back to LIKE`.
> This is a bug in `notes.py` (FTS5 virtual table missing the `tick` column), but the agent continues normally.

---

### Step 3: Wait for the First Tick to Fire

**Action**: Wait 10 seconds, then check that at least one tick has been attempted:

```bash
sleep 10
grep -c "tick" logs/persona-001.log
```

**Expected Result**: Count is ≥ 1; the LLM tick loop has fired at least once.

**Verification**:
- [x] No `"Tick error for ember-owl"` error lines
- [x] No Python exception traceback

> **Run 2026-04-18 (full pass)**: 13+ HTTP 200 responses to `api.anthropic.com/v1/messages`
> observed within 65 s (~1 tick every 5–7 s including LLM latency). No tick errors, no tracebacks.

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
Agent ember-owl idle (10 ticks), skipping LLM tick
```

**Verification**:
- [x] Idle-skip log line is present (or LLM responded with non-idle actions, which is also acceptable)
- [x] Agent process is still running (`ps` / Task Manager confirm)

> **Run 2026-04-18 (full pass)**: LLM responded actively on every tick for >65 s (no `DO_NOTHING`
> ticks observed). Per test spec, continued LLM activity is an acceptable outcome for Step 4.

---

### Step 5: Graceful Shutdown

**Action**: Send SIGINT to the agent process (Ctrl-C in its terminal).

**Expected Result**: Scheduler stops cleanly and logs shutdown.

**Verification**:
- [x] Log line contains `"Tick scheduler stopped for ember-owl"`
- [x] Process exits after SIGINT with no unhandled exception (Windows may report exit code 1)

> **Run 2026-04-18 (pass with Windows note)**: Process exits when signalled.
> `"Tick scheduler stopped"` log is produced only via interactive Ctrl+C (the asyncio SIGINT handler
> at `server.py:437` is correctly registered). Automation via `os.kill(SIGTERM)` or
> `CTRL_BREAK_EVENT` on Windows calls `TerminateProcess()` / non-catchable break — bypassing the
> Python signal handler and the graceful log. Code review confirms the handler path is correct.
>
> **Interactive confirmation (2026-04-18, terminal-assisted)**: Direct foreground run on port `50344`
> followed by interactive Ctrl+C produced:
> `Shutting down agent server...`, `Tick scheduler stopped for ember-owl`,
> `Stopped tick scheduler for ember-owl`, `De-registered agent ember-owl from orchestrator`,
> `Closed memory for persona agent ember-owl`, `Agent server stopped.`.
> Terminal exit code observed: `1` on Windows PowerShell.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Agent starts; no early traceback | ☑ |
| 2 | Tick scheduler startup log present with correct interval | ☑ |
| 3 | At least one tick fired; no tick errors | ☑ |
| 4 | Idle-skip log appears after 10 idle ticks (or LLM stays active) | ☑ |
| 5 | Graceful shutdown; scheduler-stopped log present | ☑ (interactive Ctrl+C; see note) |

---

## Edge Cases & Error Scenarios

### Edge Case 1: `ANTHROPIC_API_KEY` Not Set

**Scenario**: Environment variable absent when agent starts.

**Expected Behavior**: LLM client raises a configuration error on the first tick; log shows
`"Tick error for ember-owl"` with an authentication exception. Agent does not crash the process
— subsequent ticks are attempted (and will also fail until the key is provided).

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-04-18 | mkhomutov | Windows 11 | Partial | Step 1 startup verified with `PYTHONPATH=agents/generated --port 50055`. Agent starts; gRPC binds; tick scheduler and FTS5 log lines confirmed. Steps 2–5 (tick fires, idle, graceful shutdown) not exercised — require `ANTHROPIC_API_KEY`. Code issue: `make run-agent` fails without PYTHONPATH; port 50051 occupied in test environment. |
| 2026-04-18 | mkhomutov | Windows 11 | Partial | Retest — Steps 1–2 pass. `make run-agent` PYTHONPATH issue resolved (Makefile fix). Port 50051 still in use; used `--port 50056`. Agent starts cleanly: gRPC on 50056, FTS5 enabled, `"Tick scheduler started for ember-owl (interval=60s, idle_after=10)"`. Steps 3–5 require `ANTHROPIC_API_KEY`. |
| 2026-04-18 | mkhomutov | Windows 11 | Pass | Full live pass with `ANTHROPIC_API_KEY`. Steps 1–2: agent starts cleanly, scheduler log present `(interval=5s, idle_after=10)`. Step 3: 13+ LLM ticks fired (HTTP 200), no errors. Step 4: LLM stayed active throughout (no idle; acceptable per spec). Step 5: process exits on signal; graceful `"Tick scheduler stopped"` log requires interactive Ctrl+C on Windows (code-reviewed correct). **Bug noted**: `Notes FTS5 query failed ... no such column: tick` (non-fatal LIKE fallback). |
| 2026-04-18 | GitHub Copilot | Windows 11 | Pass | Executed in-session with `ANTHROPIC_API_KEY`. Ran on ports `50345` (`tee`-captured run) and `50346` (direct foreground). Step 1 PASS: startup clean, `Tick scheduler started ... (interval=5s, idle_after=10)` observed. Step 3 PASS: repeated LLM ticks with `HTTP/1.1 200 OK` (7 in captured log, 20+ in direct run), no tick errors/tracebacks. Step 4 PASS (acceptable variant): no idle-skip because LLM remained active. Step 5 PASS: interactive Ctrl+C produced `Tick scheduler stopped for ember-owl` and full shutdown sequence; Windows exit code `1` observed. |

---

## Notes

- Restore `tick_interval_seconds: 60` in `config/agents.yaml` after completing this test.
- Remove the log file after testing: `rm logs/persona-001.log` (the file is `.gitignore`d via
  `logs/`; verify with `git check-ignore logs/persona-001.log` if unsure).
- A single `"Tick error"` during startup (before the LLM client initialises) is acceptable;
  repeated errors on every tick indicate a configuration problem.
