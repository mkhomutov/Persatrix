# Manual Test MT-CHANNEL-004: Human-mentions-agent end-to-end (live LLM reply)

**Test ID**: `MT-CHANNEL-004`
**Feature Area**: Channels (human-in-the-loop)
**Version**: 1.0
**Created**: 2026-05-09
**Last Updated**: 2026-05-09
**Status**: Active

---

## Overview

**Purpose**: Verify the v0.3.0 user-facing channel promise — *"a human and an
agent exchange messages in a shared channel and the agent replies"* — end-to-end
against a docker-composed orchestrator + persona agent. Exercises the full
publish → fanout → response-gate → LLM action → publish-back loop on a single
mention.

**Scope**: `persatrix channel send --mention ember-owl` from a human user;
agent ingests the `CHANNEL_MESSAGE` event, the response gate (RFC 0011 §D)
admits it because the human's mention matches the agent id, the persona runtime
issues a `SEND_CHANNEL_MESSAGE` action, and the reply lands as a new message in
the same channel — visible via `persatrix channel history`.

**Out of Scope**: response-gate suppression on non-mentions
([MT-CHANNEL-002 Step 7](MT-CHANNEL-002.md) covers self-mention drop;
gate-suppress-then-`channel.messages.gated` is asserted at the unit level by
`tests/unit/python/test_channel_message_runtime.py`). Multi-agent reply
cascades — only `ember-owl` is configured as a persona in
[config/agents.yaml](../../config/agents.yaml) on the default compose stack.
Memory-recall awareness in the reply is covered by the Phase 3 integration
tests landed alongside RFC 0011 PR 5.

---

## Related Documentation

- [docs/rfcs/0011-channels-bridges.md](../rfcs/0011-channels-bridges.md) §D
  (response gate), §F (human participation)
- [docs/rfcs/0011-channels-bridges.md OQ #7](../rfcs/0011-channels-bridges.md#open-questions)
  — human gate-bypass (option **b**: humans subject to the same gate, must `@`-mention)
- [docs/guides/channels.md](../guides/channels.md) — human walkthrough this MT scripts

**Related Automated Tests**:
- `tests/unit/python/test_channel_message_runtime.py` — gate semantics (`when_mentioned` admit/suppress)
- `tests/integration/test_channel_history_awareness.py` — agent-B reply demonstrates channel-history awareness

---

## Preconditions

Same as [MT-CHANNEL-001 § Preconditions](MT-CHANNEL-001.md#preconditions),
**plus**:

- ☐ `.env` carries a valid `ANTHROPIC_API_KEY` (the persona reply is a real LLM
  call; absence of the key fails the agent container at startup, not at the
  reply boundary)
- ☐ Default `config/agents.yaml` (with `ember-owl` declared) — no edits needed

This test is independent of MT-CHANNEL-001/002/003 — it creates and tears down
its own channel.

---

## Test Procedure

### Step 1: Create a group channel with the human and the persona as members

**Action**:

```pwsh
$body = @'
{
  "name":"mt-channel-004",
  "members":[
    {"id":"ember-owl","respond":"when_mentioned"},
    {"id":"alice","respond":"when_mentioned"}
  ]
}
'@
(Invoke-WebRequest -Uri http://127.0.0.1:8080/api/v1/channels `
    -Method POST -ContentType 'application/json' -Body $body -UseBasicParsing).Content
```

**Expected**:
- HTTP 200/201 with body containing `"id":"group:mt-channel-004"`,
  `"channel_type":"group"`, and `members` array with both ids.

**Verification**:
- [ ] Response body contains both `"ember-owl"` and `"alice"` in `members`.

---

### Step 2: Human posts a top-level message that mentions the persona

**Action**:

```pwsh
./bin/persatrix.exe channel send mt-channel-004 `
    "ember-owl, what's one engineering principle you live by?" `
    --as alice --mention ember-owl --json
```

**Expected**:
- Exit 0; stdout is a single-line `ChannelMessage` with `sender_id: "alice"`
  and `mentions: ["ember-owl"]`.
- The orchestrator fans this out to `ember-owl` via gRPC `ReceiveChannelMessage`
  (covered by `internal/channels/grpc_dispatcher_test.go`).

**Verification**:
- [ ] Stored message's `mentions` array equals `["ember-owl"]`.

---

### Step 3: Wait for the agent's reply, then read history

The persona's reply travels through the action loop → `SEND_CHANNEL_MESSAGE`
action → `POST /api/v1/channels/{id}/messages` and lands in the same channel
within a few seconds (LLM round-trip dominated; `ember-owl` runs on
`claude-sonnet-4-20250514` by default — see
[config/agents.yaml](../../config/agents.yaml)).

**Action**:

```pwsh
Start-Sleep -Seconds 15
./bin/persatrix.exe channel history mt-channel-004 --limit 5
./bin/persatrix.exe channel history mt-channel-004 --limit 5 --json | ConvertFrom-Json
```

**Expected**:
- `history` shows at least two rows newest-first; the freshest is from
  `ember-owl` and contains a non-empty `content`.
- The reply's `mentions` array is empty (or contains `alice` if the persona
  decided to address the human by name — both are acceptable; the gate fires on
  presence of the agent's own id, not on echoing the original).

**Verification**:
- [ ] Most recent message has `sender_id == "ember-owl"`.
- [ ] Reply `content` is non-empty (sanity check — content quality is not asserted).

---

### Step 4: Confirm the gate suppressed nothing on the non-mention case

**Action**:

```pwsh
# Capture the message count, send a non-mentioning message, wait, recount.
$beforeJson = ./bin/persatrix.exe channel history mt-channel-004 --limit 50 --json
$beforeCount = ($beforeJson | ConvertFrom-Json).Count

./bin/persatrix.exe channel send mt-channel-004 `
    "Just thinking out loud, no reply expected." --as alice

Start-Sleep -Seconds 8
$afterJson = ./bin/persatrix.exe channel history mt-channel-004 --limit 50 --json
$afterCount = ($afterJson | ConvertFrom-Json).Count

"before=$beforeCount after=$afterCount"
```

**Expected**:
- `$afterCount` is exactly `$beforeCount + 1` — only the human's new message
  was added; the agent's `respond: when_mentioned` policy gated the event.

**Verification**:
- [ ] `after - before == 1`.
- [ ] `docker compose logs orchestrator --tail 20` shows no
  `SEND_CHANNEL_MESSAGE` action for this turn.

---

### Step 5: Teardown

**Action**:

```pwsh
(Invoke-WebRequest -Method DELETE `
    -Uri http://127.0.0.1:8080/api/v1/channels/group:mt-channel-004 `
    -UseBasicParsing).StatusCode
```

**Expected**:
- HTTP 204 No Content.

**Verification**:
- [ ] `./bin/persatrix.exe channel list` no longer shows the channel.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Channel created with both members | ☐ |
| 2 | Human send with mention round-trips and stores | ☐ |
| 3 | Agent reply lands in history within ~15s | ☐ |
| 4 | Non-mention message produces no agent reply | ☐ |
| 5 | DELETE returns 204 and channel list no longer shows it | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Reply slower than 15s

The 15-second sleep in Step 3 is a comfortable upper bound for a single
`claude-sonnet-4` round-trip on a healthy network, but a cold agent process
or backed-up tick scheduler can push it longer. If `history` after 15s shows
only the human turn, repeat the read up to two more times at 5s intervals
before declaring Fail. Persistent absence of an agent turn → check
`docker compose logs ember-owl --tail 50` for a `SEND_CHANNEL_MESSAGE` action
or an LLM error.

### Edge Case 2: Mention typo silently ignored

`channel send … --mention ember_owl` (underscore) does not raise an error —
mentions are routing hints, not membership constraints
([MT-CHANNEL-002 Edge Case 2](MT-CHANNEL-002.md#edge-case-2-mention-id-that-is-not-a-channel-member)).
The agent will not reply because the gate compares ids exactly. If the test
agent fails to reply, double-check the `--mention` value before re-running.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|

---

## Notes

- The full v0.3.0 user-facing promise — *"give agents a shared channel and
  watch them talk, negotiate, and form opinions over time"* — needs at least
  two persona agents to demonstrate the inter-agent half. Operators who want
  to exercise that surface should add a second `type: persona` entry in
  [config/agents.yaml](../../config/agents.yaml) (e.g. duplicate `ember-owl`
  with a different id and persona block) and rerun this test with both as
  members. The single-persona shape pinned here is the tightest reproducible
  shape on the default compose stack.
- Channels are unauthenticated in v0.3.0 (RFC 0011 §C trust boundary); the
  orchestrator emits a one-shot startup `WARN` to that effect — see
  [docs/guides/channels.md](../guides/channels.md) for operator guidance.
