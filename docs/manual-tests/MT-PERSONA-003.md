# Manual Test MT-PERSONA-003: Empty-Context TICK Short-Circuit Suppresses LLM Calls

**Test ID**: `MT-PERSONA-003`
**Feature Area**: Persona
**Version**: 1.0
**Created**: 2026-04-22
**Last Updated**: 2026-04-22
**Status**: Active

---

## Overview

**Purpose**: Verify that a freshly-loaded persona agent with empty memory and no pending
conversation turn or active goal payload short-circuits autonomous TICK events without invoking
the LLM, per RFC 0017
[Section F](../rfcs/0017-persona-memory-injection-budget.md#f-empty-context-tick-short-circuit).
This closes the cold-start cost-drain window where an idle agent would otherwise issue up to
`idle_after_ticks` (default 10) full LLM calls before idle suppression engaged.

**Scope**: TICK handler short-circuit guard in `agents/persona_runtime/action_loop.py`,
`empty_context_tick` DEBUG log emission, `idle_count` advancement on suppressed ticks, absence
of LLM HTTP traffic during the suppression window.

**Out of Scope**: LLM response correctness on non-suppressed ticks (covered by
[MT-PERSONA-001](MT-PERSONA-001.md)); inbound message handling (covered by
[MT-PERSONA-002](MT-PERSONA-002.md)); allocator behaviour (covered by
[MT-MEMORY-004](MT-MEMORY-004.md)).

---

## Related Documentation

**Feature Documentation**:
- [docs/rfcs/0017-persona-memory-injection-budget.md](../rfcs/0017-persona-memory-injection-budget.md) — §F Empty-Context TICK Short-Circuit
- [agents/persona_runtime/action_loop.py](../../agents/persona_runtime/action_loop.py) — `_ActionLoopMixin._on_event_inner` short-circuit guard
- [agents/tick.py](../../agents/tick.py) — `TickScheduler.idle_count`

**Related Automated Tests**:
- Unit tests: `agents/tests/test_persona_tick_shortcircuit.py`

---

## Preconditions

### System Requirements

**Operating Systems**:
- ☐ Windows 10/11 (x64)
- ☐ macOS 12.0+ (Intel/Apple Silicon)
- ☐ Linux (Ubuntu 22.04+)

**Dependencies Installed**:
- Python 3.11+: `python3 --version`
- Agents package installed: `make build-agents`
- `ANTHROPIC_API_KEY` is **not required** for this test — the short-circuit fires before any LLM
  call is issued. (If a key is set, the agent must still produce zero outbound calls to
  `api.anthropic.com` during the empty-context window; observe and confirm.)

### Application State

- ☐ Orchestrator running: `make run` (binds to `127.0.0.1:8080`)
- ☐ Config valid: `make validate` exits 0
- ☐ No prior episodic / relationship / notes data for the test agent — see [Step 1](#step-1-purge-prior-memory-for-the-test-agent).

### Test Data

Create the log directory and reduce the persona tick interval for a faster test, exactly as in
[MT-PERSONA-001](MT-PERSONA-001.md) preconditions:

```bash
mkdir -p logs
```

In `config/agents.yaml` for the `ember-owl` persona:

```yaml
autonomy:
  tick_interval_seconds: 5   # test value; restore to 60 after testing
  idle_after_ticks: 10       # default; do not change
```

---

## Test Procedure

### Step 1: Purge Prior Memory for the Test Agent

**Action**: Remove any persisted episodic / notes / relationship database for `ember-owl` so the
agent starts cold:

```bash
# Adjust path to match your environment's memory store location.
rm -f data/agents/ember-owl/*.db data/agents/ember-owl/*.db-wal data/agents/ember-owl/*.db-shm
```

**Expected Result**: No `ember-owl` memory files remain.

**Verification**:
- [ ] `ls data/agents/ember-owl/ 2>/dev/null` lists nothing or the directory does not exist

---

### Step 2: Start the Persona Agent with DEBUG Logging Enabled

**Action**: In a dedicated terminal, start `ember-owl` with `DEBUG`-level logging so the
short-circuit log entries are visible:

```bash
PERSATRIX_LOG_LEVEL=DEBUG make run-agent AGENT=ember-owl 2>&1 | tee logs/persona-003.log
```

> If your environment ignores `PERSATRIX_LOG_LEVEL`, run the agent directly with `--log-level
> debug` or set `LOG_LEVEL=DEBUG`. The exact env var depends on your local agent runner; the
> requirement is simply that `logger.debug` calls in `agents/persona_runtime/action_loop.py`
> reach the captured log stream.

**Expected Result**: Agent starts cleanly; tick scheduler initialises with `interval=5s,
idle_after=10`.

**Verification**:
- [ ] Log line `Tick scheduler started for ember-owl (interval=5s, idle_after=10)` is present
- [ ] No Python traceback in the first 5 seconds

---

### Step 3: Wait for ≥ 10 Tick Intervals and Confirm Short-Circuit Fires

**Action**: Wait 60 seconds (≥ 10 ticks at the test interval), then inspect the log:

```bash
sleep 60
grep -c "empty_context_tick" logs/persona-003.log
```

**Expected Result**: At least one `empty_context_tick` log entry per fired tick. With a 5-second
interval and a 60-second wait, expect roughly 10–12 hits before idle suppression engages.

**Verification**:
- [ ] Count is `>= 1`
- [ ] At least one log line matches: `Agent ember-owl: empty-context tick suppressed`
- [ ] The log entry includes the `extra` field `reason=empty_context_tick` (visible if the log
      formatter renders `extra` fields; otherwise confirm via the message text)

---

### Step 4: Confirm Zero LLM HTTP Calls During the Empty-Context Window

**Action**: Inspect the log for outbound HTTP traffic to the LLM API during Step 3's window:

```bash
grep -E "api\.anthropic\.com|HTTP/1\.1 200" logs/persona-003.log | wc -l
```

**Expected Result**: `0`. The short-circuit must fire before any LLM call; no outbound HTTP
should appear for any of the suppressed ticks.

**Verification**:
- [ ] Count is `0` (or matches only unrelated HTTPX startup probes — none expected)
- [ ] No `Tick error for ember-owl` lines

---

### Step 5: Confirm Idle Suppression Engages on Cold-Start

**Action**: After Step 3's 60-second wait, look for the `TickScheduler` idle-skip log:

```bash
grep "idle" logs/persona-003.log | tail -5
```

**Expected Result**: After 10 consecutive `DO_NOTHING` returns (which the short-circuit produces),
`TickScheduler` enters its idle-skip mode:

```
Agent ember-owl idle (10 ticks), skipping LLM tick
```

**Verification**:
- [ ] Idle-skip log line is present within ~50 seconds of agent start (10 ticks × 5 s)
- [ ] The line appears **without** any preceding successful LLM tick

---

### Step 6: Wake the Agent and Confirm Short-Circuit Disengages

**Action** *(optional; requires `ANTHROPIC_API_KEY`)*: Send an inbound message to the agent and
confirm the short-circuit yields to a real LLM call:

```bash
grpcurl -plaintext \
  -import-path proto/ \
  -proto agent_message.proto \
  -d '{
    "message_id": "test-msg-mt-persona-003",
    "channel_id": "general",
    "sender_id": "tester",
    "type": "TEXT",
    "content": "What is your current focus?"
  }' \
  localhost:50054 \
  persatrix.v1.ChannelService/SendMessage
```

**Expected Result**: A `MESSAGE_RECEIVED` event flows through `_on_event_inner`; the
short-circuit guard does **not** fire (event is not a TICK). The LLM is invoked normally.

**Verification**:
- [ ] `"delivered": true` in the gRPC response
- [ ] At least one new `api.anthropic.com` / `HTTP/1.1 200` entry appears in the log after the
      message is sent
- [ ] No new `empty_context_tick` log between message receipt and reply

---

### Step 7: Graceful Shutdown

**Action**: Send SIGINT to the agent process (Ctrl-C in its terminal).

**Expected Result**: Same shutdown sequence as [MT-PERSONA-001](MT-PERSONA-001.md) Step 5.

**Verification**:
- [ ] Log line contains `Tick scheduler stopped for ember-owl`
- [ ] Process exits with no unhandled exception (Windows may report exit code 1)

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Memory store is empty | ☐ |
| 2 | Agent starts; scheduler log present | ☐ |
| 3 | ≥ 1 `empty_context_tick` log entries during the wait window | ☐ |
| 4 | Zero LLM HTTP calls during the empty-context window | ☐ |
| 5 | Idle-skip log appears after 10 short-circuited ticks | ☐ |
| 6 | (Optional) Inbound message bypasses short-circuit; LLM call observed | ☐ |
| 7 | Graceful shutdown | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Active Goal Payload Present at Cold-Start

**Scenario**: The agent boots with `PersonaState.goal_progress` populated (e.g., a long-running
goal carried across a restart).

**Expected Behavior**: The short-circuit guard's `_has_active_goal_payload()` check returns
`True`; the LLM is invoked normally on every TICK. No `empty_context_tick` log entries are
emitted.

### Edge Case 2: Pending Conversation Turn at Cold-Start

**Scenario**: A `MESSAGE_RECEIVED` event arrived during shutdown and was persisted, leaving
`PersonaState.recent_context` populated when the agent restarts.

**Expected Behavior**: `_has_pending_turn()` returns `True`; the LLM is invoked normally on the
first TICK that surfaces the pending turn. The short-circuit does not fire.

### Edge Case 3: DEBUG Logging Not Enabled

**Scenario**: Tester forgot to set `PERSATRIX_LOG_LEVEL=DEBUG`.

**Expected Behavior**: The short-circuit still fires (it is unconditional), but Step 3 produces
zero `empty_context_tick` matches because the log entries are below the active log level. Step 4
(zero LLM HTTP calls) and Step 5 (idle-skip after 10 ticks) still pass and are sufficient
indirect evidence that the short-circuit is firing.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| YYYY-MM-DD | [Name] | [OS] | Pass/Fail | [Notes] |

---

## Notes

- Restore `tick_interval_seconds: 60` in `config/agents.yaml` after completing this test.
- Remove the log file after testing: `rm logs/persona-003.log`.
- The cost saving at fleet scale is the practical value being verified here. RFC 0017
  [Section F](../rfcs/0017-persona-memory-injection-budget.md#f-empty-context-tick-short-circuit)
  estimates ~$0.01 per cold-loaded idle agent at a representative ~2 k-token persona prompt;
  this test confirms the saving is real (zero LLM calls during the suppression window).
- RFC 0019 (OTEL Completion) may later promote the `empty_context_tick` log entry to a counter.
  When that lands, this test should add a Step asserting the counter increments.
