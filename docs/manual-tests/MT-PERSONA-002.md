# Manual Test MT-PERSONA-002: Persona Handles an Inbound Channel Message and Produces a Logged Response

**Test ID**: `MT-PERSONA-002`
**Feature Area**: Persona
**Version**: 1.0
**Created**: 2026-04-18
**Last Updated**: 2026-04-18
**Status**: Active

---

## Overview

**Purpose**: Verify that a running persona agent receives an inbound `AgentMessage` via the
`ChannelService`, processes it, and logs the response; and that the tick scheduler's idle counter
is reset (woken) on receipt of the message.

**Scope**: `ChannelService.SendMessage` gRPC call, persona event processing, idle-wake behaviour.

**Out of Scope**: LLM response quality; episodic memory writes triggered by the event.

---

## Related Documentation

**Feature Documentation**:
- [proto/agent_message.proto](../../proto/agent_message.proto) — `ChannelService`, `AgentMessage`
- [docs/rfcs/0005-persona-agent-memory.md](../rfcs/0005-persona-agent-memory.md)

**Related Automated Tests**:
- Integration tests: `tests/integration/`

---

## Preconditions

### System Requirements

**Operating Systems**:
- ☐ Windows 10/11 (x64)
- ☐ macOS 12.0+ (Intel/Apple Silicon)
- ☐ Linux (Ubuntu 22.04+)

**Dependencies Installed**:
- Python 3.11+: `python3 --version`
- `grpcurl` 1.8+: `grpcurl --version`
- `ANTHROPIC_API_KEY` set in environment
- Agents package installed: `make build-agents`

### Application State

- ☐ Orchestrator running: `make run`
- ☐ `sarah-chen` persona agent running (see MT-PERSONA-001 Step 1)
- ☐ Agent gRPC port known — default `50054` for persona agents; confirm in startup logs or
  `config/agents.yaml`

---

## Test Procedure

### Step 1: Confirm Agent is Listening

**Action**: Health-check the persona agent's gRPC endpoint:

```bash
grpcurl -plaintext \
  -import-path proto/ \
  -proto task.proto \
  localhost:50054 \
  persatrix.v1.AgentService/HealthCheck
```

**Expected Result**: Response with `"status": "SERVING"`.

**Verification**:
- [ ] `grpcurl` exits 0
- [ ] Response JSON contains `"status": "SERVING"`

---

### Step 2: Send an Inbound Channel Message

**Action**: Send an `AgentMessage` addressed to the agent's default channel:

```bash
grpcurl -plaintext \
  -import-path proto/ \
  -proto agent_message.proto \
  -d '{
    "message_id": "test-msg-001",
    "channel_id": "general",
    "sender_id": "tester",
    "type": "TEXT",
    "content": "Sarah, what is your current focus?"
  }' \
  localhost:50054 \
  persatrix.v1.ChannelService/SendMessage
```

**Expected Result**: Response confirms delivery:

```json
{"messageId": "test-msg-001", "delivered": true}
```

**Verification**:
- [ ] `"delivered": true` in response
- [ ] `grpcurl` exits 0

---

### Step 3: Verify Event Processing in Agent Logs

**Action**: Within 30 seconds of Step 2, inspect the agent log (from MT-PERSONA-001 Step 1's
`tee` file, or the terminal where the agent is running):

```bash
grep -E "event|message|sarah-chen" logs/persona-001.log | tail -20
```

**Expected Result**: Log entries show the agent received and began processing the event. Exact
message text depends on implementation, but should include the agent ID and event content.

**Verification**:
- [ ] A log entry referencing `"sarah-chen"` and the event appears within 30 s
- [ ] No `"event processing timed out"` error logged
- [ ] No Python exception traceback

---

### Step 4: Verify Tick Scheduler Wake

**Action**: Check that the idle counter was reset — if the agent was idle before the message, the
next tick should fire immediately rather than being skipped:

```bash
grep "idle" logs/persona-001.log | tail -5
```

**Expected Result**: Any `"idle"` skip messages stop appearing in the log immediately after the
message is received (the wake signal resets the counter).

**Verification**:
- [ ] No further idle-skip log lines appear within two tick intervals after Step 2
  (or the agent was not idle and idle skips were never present — both are acceptable)

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Agent gRPC endpoint healthy | ☐ |
| 2 | Message delivered; `delivered: true` | ☐ |
| 3 | Event processing logged within 30 s; no timeout | ☐ |
| 4 | Idle counter reset; no new idle-skip lines after message | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Agent gRPC Port Mismatch

**Scenario**: Persona agent bound to a port other than 50054.

**Expected Behavior**: `grpcurl` returns a connection-refused error. Check the agent startup log
for `"gRPC server listening on"` to find the actual port and retry.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-04-18 | mkhomutov | Windows 11 | Not run | Requires `ANTHROPIC_API_KEY` and a live persona agent. MT-PERSONA-001 code issue (PYTHONPATH + port conflict) must be resolved first. |
| 2026-04-18 | mkhomutov | Windows 11 | Not run | Retest — PYTHONPATH Makefile issue now resolved. Still requires `ANTHROPIC_API_KEY` and a persona agent running Steps 1–4 of MT-PERSONA-001. |
| 2026-04-18 | GitHub Copilot | Windows 11 | Fail | Executed with Python gRPC stub fallback (`grpcurl` not installed). Step 1 PASS: `HealthCheck` returned `status=1` (SERVING) on `127.0.0.1:50345`. Step 2 FAIL: `ChannelService/SendMessage` returns gRPC `StatusCode.UNIMPLEMENTED` (`Method not found!`). Steps 3-4 blocked because no inbound event is accepted by the server. Observed implementation gap: `agents/server.py` registers only `AgentService` and does not register `ChannelService`. |
| 2026-04-18 | GitHub Copilot | Windows 11 | Pass | Retest on `127.0.0.1:50354` after `ChannelService` implementation landed. Used generated Python gRPC stubs because `grpcurl` is not installed. Step 1 PASS: `HealthCheck` returned `status=1` (`SERVING`). Step 2 PASS: `SendMessage(message_id=test-msg-001)` returned `delivered=true`. Step 3 PASS: log shows the inbound content routed into the persona prompt (`Message from tester: Sarah, what is your current focus?`) and completed as `Event: message_received -> Actions: ['complete_task']`; no traceback and no `event processing timed out`. Step 4 PASS (acceptable variant): no idle-skip lines appeared after the message because the agent was active rather than idle. Non-blocking warnings observed: episodic/notes FTS5 query fallback triggered by punctuation in the inbound message. |

---

## Notes

- **Known Gap (2026-04-18)**: `ChannelService` is declared in `proto/agent_message.proto`, but the current persona server path does not register it; `SendMessage` currently returns gRPC `UNIMPLEMENTED` (`Method not found!`).
- The persona agent's gRPC port is configurable. If the default differs from 50054, update the
  `grpcurl` commands with the correct port.
- All `grpcurl` commands use `-import-path proto/ -proto <file>` to resolve method descriptors
  without requiring server-side gRPC reflection. Run them from the **repo root** so that the
  `proto/` path resolves correctly.
- If `grpcurl` is not available, the equivalent test can be run with a small Python script using
  the generated `msgpb` stubs in `agents/generated/`.
