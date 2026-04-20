# Manual Test MT-CHAT-004: User-Agent Relationship — Trust Score Evolves After Chat Exchanges

**Test ID**: `MT-CHAT-004`
**Feature Area**: Chat
**Version**: 1.0
**Created**: 2026-04-20
**Last Updated**: 2026-04-20
**Status**: Active

---

## Overview

**Purpose**: Verify that chatting with a persona agent via the REST chat endpoint causes the
agent's relationship memory to record interactions and that trust scores and interaction counts
are updated accordingly for the user participant.

**Scope**: `RelationshipMemory.record_interaction()` called during `SendChatMessage`, interaction
count tracking, trust score persistence, and `get_relationship_summary()` for user participants.

**Out of Scope**: REST endpoint shape validation (MT-CHAT-001); CLI mechanics (MT-CHAT-002);
session continuity (MT-CHAT-003); trust decay (`apply_decay`) — covered by MT-MEMORY-002.

---

## Related Documentation

**Feature Documentation**:
- [agents/memory/relationship.py](../../agents/memory/relationship.py) — relationship memory
- [agents/server_servicers.py](../../agents/server_servicers.py) — `SendChatMessage` records interaction
- [agents/participant.py](../../agents/participant.py) — `UserParticipant`, `UserStore`
- [docs/rfcs/0005-persona-agent-memory.md](../rfcs/0005-persona-agent-memory.md)

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
- Go 1.24+: `go version`
- Python 3.11+: `python3 --version`
- `curl` available in PATH

### Application State

- ☐ Orchestrator running: `make run`
- ☐ At least one persona agent registered and healthy (e.g. `ember-owl`): `make run-agent`
- ☐ Config files valid: `make validate`
- ☐ `ANTHROPIC_API_KEY` set in environment
- ☐ Clean relationship state: remove any pre-existing relationship memory for the test user
  (or use a fresh `user_id` not used in other tests):

```bash
# Option A: use a unique user_id for this test
# user_id = "mt-chat-004-user" (used throughout this test)

# Option B: remove stale DB files if re-running
rm -f data/mt-chat-004-rel.db data/mt-chat-004-rel.db-shm data/mt-chat-004-rel.db-wal
```

### Test Data

No external fixtures. All interaction is via `curl` and inline Python scripts.

---

## Test Procedure

### Step 1: Verify Baseline — No Prior Interactions

**Action**: Query the agent's relationship memory for the test user before any chat:

```bash
python3 - <<'EOF'
import asyncio
from persatrix_agents.memory.relationship import RelationshipMemory

async def main():
    mem = RelationshipMemory("ember-owl")
    await mem.initialize()

    summary = await mem.get_relationship_summary(
        "mt-chat-004-user",
        participant_type="agent",
        other_participant_type="user",
    )

    if summary is None:
        print("No prior relationship — baseline clean")
        print("Trust: 0.5 (default)")
        print("Interactions: 0")
    else:
        print(f"Pre-existing relationship found:")
        print(f"  Trust: {summary.trust_score}")
        print(f"  Interactions: {summary.interaction_count}")

    await mem.close()

asyncio.run(main())
EOF
```

**Expected Result**: No prior relationship exists, or if the agent's database already has
entries, record the baseline interaction count for comparison in later steps.

**Verification**:
- [ ] Baseline recorded: trust score and interaction count noted
- [ ] If clean: `"No prior relationship — baseline clean"` printed

---

### Step 2: Send 5 Chat Messages

**Action**: Send 5 messages to the agent with the test user. Capture the `session_id` from the
first response and reuse it:

```bash
# Message 1
RESP=$(curl -s -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello! I am starting a new project today.", "user_id": "mt-chat-004-user"}')
echo "$RESP"
SESSION_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")

# Messages 2–5
for i in 2 3 4 5; do
  curl -s -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
    -H "Content-Type: application/json" \
    -d "{\"message\": \"This is test message number $i for the relationship memory test.\", \"user_id\": \"mt-chat-004-user\", \"session_id\": \"$SESSION_ID\"}"
  echo ""
done
```

**Expected Result**: All 5 requests return HTTP 200 with `reply_status: "ok"`.

**Verification**:
- [ ] All 5 responses are HTTP 200
- [ ] All 5 responses have `reply_status: "ok"` (or `"empty"` is acceptable if the agent
  occasionally produces no reply)
- [ ] Agent replies are coherent (not error messages)

---

### Step 3: Verify Interaction Count Increased

**Action**: Query the relationship memory and confirm the interaction count reflects the
5 chat exchanges:

```bash
python3 - <<'EOF'
import asyncio
from persatrix_agents.memory.relationship import RelationshipMemory

async def main():
    mem = RelationshipMemory("ember-owl")
    await mem.initialize()

    summary = await mem.get_relationship_summary(
        "mt-chat-004-user",
        participant_type="agent",
        other_participant_type="user",
    )

    if summary is None:
        print("FAIL — no relationship record found after 5 chat messages")
    else:
        print(f"Trust score: {summary.trust_score}")
        print(f"Interaction count: {summary.interaction_count}")
        print(f"Last interaction at: {summary.last_interaction_at}")
        print(f"Other participant type: {summary.other_participant_type}")
        print(f"Recent interactions: {len(summary.recent_interactions)}")

        for ix in summary.recent_interactions:
            print(f"  [{ix.id[:8]}] type={ix.interaction_type} "
                  f"other_type={ix.other_participant_type}")

        assert summary.interaction_count >= 5, (
            f"Expected >= 5 interactions, got {summary.interaction_count}"
        )
        assert summary.other_participant_type == "user", (
            f"Expected other_participant_type='user', got '{summary.other_participant_type}'"
        )
        print("PASS")

    await mem.close()

asyncio.run(main())
EOF
```

**Expected Result**: Interaction count is ≥ 5. Each interaction has `interaction_type="chat"`
and `other_participant_type="user"`.

**Verification**:
- [ ] `Interaction count` is ≥ 5
- [ ] `other_participant_type` is `"user"`
- [ ] `last_interaction_at` is a recent timestamp
- [ ] Recent interactions show `type=chat` and `other_type=user`
- [ ] Script prints `"PASS"`

---

### Step 4: Verify Trust Score Remains at Default (0.5)

**Action**: The trust score is checked in the output from Step 3.

**Expected Result**: Trust score is at the default `0.5` (neutral). The `SendChatMessage`
servicer calls `record_interaction()` which records the interaction but does not automatically
adjust trust — trust changes require explicit `update_trust()` calls (e.g. from agent
decision-making). A score of `0.5` confirms no erroneous drift.

**Verification**:
- [ ] Trust score is `0.5` (default neutral) — this is correct
- [ ] If trust is not `0.5`, verify whether the persona agent's decision-making explicitly
  called `update_trust()` during the chat (this is model-dependent behaviour, not a test failure)

---

### Step 5: Verify Interaction Details

**Action**: Inspect individual interaction records:

```bash
python3 - <<'EOF'
import asyncio
from persatrix_agents.memory.relationship import RelationshipMemory

async def main():
    mem = RelationshipMemory("ember-owl")
    await mem.initialize()

    summary = await mem.get_relationship_summary(
        "mt-chat-004-user",
        participant_type="agent",
        other_participant_type="user",
    )

    if summary is None or not summary.recent_interactions:
        print("FAIL — no interactions to inspect")
        await mem.close()
        return

    # Check that at least one interaction has an outcome (the agent's reply)
    has_outcome = any(ix.outcome for ix in summary.recent_interactions)
    print(f"Any interaction has outcome (agent reply): {has_outcome}")

    # Check interaction types are all "chat"
    types = {ix.interaction_type for ix in summary.recent_interactions}
    print(f"Interaction types: {types}")
    assert types == {"chat"}, f"Expected only 'chat' type, got {types}"

    # Check participant types
    other_types = {ix.other_participant_type for ix in summary.recent_interactions}
    print(f"Other participant types: {other_types}")
    assert other_types == {"user"}, f"Expected only 'user', got {other_types}"

    print("PASS")
    await mem.close()

asyncio.run(main())
EOF
```

**Expected Result**: All interactions have `interaction_type="chat"` and
`other_participant_type="user"`. At least one interaction has a non-empty `outcome` (the agent's
reply text).

**Verification**:
- [ ] All interaction types are `"chat"`
- [ ] All other participant types are `"user"`
- [ ] At least one interaction has an outcome
- [ ] Script prints `"PASS"`

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Baseline recorded (no prior relationship or count noted) | ☐ |
| 2 | All 5 chat messages return HTTP 200 | ☐ |
| 3 | Interaction count ≥ 5, other_participant_type is "user" | ☐ |
| 4 | Trust score is 0.5 (default neutral) | ☐ |
| 5 | All interactions are type "chat" with "user" participant | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Agent Reply is Empty

**Scenario**: One or more of the 5 messages returns `reply_status: "empty"`.

**Expected**: The interaction is still recorded in relationship memory (the servicer calls
`record_interaction()` even when the reply is empty). The interaction count should still
increment.

### Edge Case 2: Multiple Users Chatting

**Scenario**: Send messages from two different `user_id` values, then check that each user has
a separate relationship record.

**Expected**: `get_relationship_summary("user-a", other_participant_type="user")` and
`get_relationship_summary("user-b", other_participant_type="user")` return independent records.

### Edge Case 3: Rapid-Fire Messages

**Scenario**: Send 5 messages in quick succession (no wait between requests).

**Expected**: All interactions are recorded. No race condition causes missing records (SQLite
WAL mode handles concurrent writes).

---

## API Key Requirement

| Step | Requires `ANTHROPIC_API_KEY` |
|------|------------------------------|
| 1 | No (direct memory query) |
| 2 | Yes (agent calls LLM) |
| 3 | No (direct memory query) |
| 4 | No (checked in Step 3 output) |
| 5 | No (direct memory query) |
