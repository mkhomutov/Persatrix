# Manual Test MT-CHAT-004: User-Agent Relationship — Interaction Recorded at Conversation Close

**Test ID**: `MT-CHAT-004`
**Feature Area**: Chat
**Version**: 1.1
**Created**: 2026-04-20
**Last Updated**: 2026-05-22
**Status**: Active

---

## Overview

**Purpose**: Verify that chatting with a persona agent records a relationship interaction for the
user participant, and that the interaction count and trust score behave per the RFC 0020 model:
**one relationship interaction is recorded per *closed conversation*, not per turn**, with the
trust score left at its neutral default.

**Scope**: `RelationshipMemory.record_interaction()` called **once at interaction close** (via
[`agents/persona_runtime/record_close.py`](../../agents/persona_runtime/record_close.py)),
`interaction_count` semantics (closed conversations, not messages), `interaction_type="conversation"`,
`other_participant_type="user"`, and trust remaining at the `0.5` default.

**Out of Scope**: REST endpoint shape (MT-CHAT-001); CLI mechanics (MT-CHAT-002); session
continuity (MT-CHAT-003); trust decay `apply_decay` (MT-MEMORY-002).

> **v1.1 rewrite (RFC 0020 PR 4 interaction-close model).** The v1.0 recipe expected
> `interaction_count >= 5` after 5 chat turns and `interaction_type == "chat"`. RFC 0020 PR 4
> **removed the per-turn `record_interaction` call from `SendChatMessage`**
> ([`agents/server_servicers.py`](../../agents/server_servicers.py) carries the explicit comment).
> The relationship row is now bumped **once per closed interaction**, with
> `interaction_type="conversation"`. A 5-turn conversation that closes once yields
> `interaction_count == 1`, not 5.

---

## How relationship interactions are recorded now (read first)

Per [RFC 0020](../rfcs/0020-interaction-lifecycle.md), `RelationshipMemory.record_interaction()`
is called **once, at interaction close**, in the background finalize path
([`record_close.py`](../../agents/persona_runtime/record_close.py)): `interaction_type="conversation"`,
`outcome` = the conversation summary, for `dm:` scopes only (human chat is a `dm:<agent>:<user>`
scope, so it fires). It increments `interaction_count` by 1 and updates `last_interaction_at`.

It does **not** change `trust_score` — trust moves only via the separate `update_trust()` /
`apply_decay()` paths. RFC 0020's "trust delta from interaction outcome" is deferred (post-RFC
MQ-1), so a closed chat leaves `trust_score` at the `0.5` default for a fresh peer.

Close itself is **idle-gap-timeout only** on the live stack (no explicit end-chat endpoint),
materialized by the next event — see [MT-CHAT-003 § How an interaction closes](MT-CHAT-003.md#how-an-interaction-closes-read-first).

---

## Related Documentation

**Feature Documentation**:
- [docs/rfcs/0020-interaction-lifecycle.md](../rfcs/0020-interaction-lifecycle.md) — interaction model + migration notes.
- [agents/persona_runtime/record_close.py](../../agents/persona_runtime/record_close.py) — `record_closed_interaction` (the single per-close bump).
- [agents/server_servicers.py](../../agents/server_servicers.py) — `SendChatMessage` (per-turn `record_interaction` **removed**, RFC 0020 PR 4).
- [agents/memory/relationship.py](../../agents/memory/relationship.py) — `RelationshipMemory`, `get_relationship_summary`, `update_trust`.

**Related Automated Tests**:
- Integration: `tests/integration/test_summarize_on_close.py` (`TestRecordInteractionMove`: 11 turns → `interaction_count == 1`).

**Related Manual Tests**:
- [MT-CHAT-003](MT-CHAT-003.md) — episodic episode written at close (same lifecycle).

---

## Preconditions

### System Requirements

**Operating Systems**:
- ☐ Windows 10/11 (x64) · macOS 12.0+ · Linux (Ubuntu 22.04+)

**Dependencies Installed**:
- Docker Desktop (the documented stack), or a local `make run` + `make run-agent`.
- `curl` + `jq` available in PATH.
- `ANTHROPIC_API_KEY` available (via `.env` for Docker, or the shell for a local run).

### Application State

- ☐ Orchestrator + persona agent (e.g. `ember-owl`) running and healthy.
- ☐ Config valid: `make validate` exits 0.

### Test Data — short idle timeout + fresh user

As in [MT-CHAT-003](MT-CHAT-003.md#test-data--shorten-the-idle-timeout), temporarily set
`interaction_idle_timeout_sec: 15` under the persona's `memory:` block in `config/agents.yaml`,
`make validate`, and restart the persona. Use a fresh `USER_ID="mt-chat-004-user"` so the baseline
is clean.

---

## Test Procedure

### Step 1: Baseline — No Prior Relationship

**Action**:

```bash
docker exec -i persatrix-agent-ember-owl-1 python - <<'PY'
import sqlite3
c = sqlite3.connect("/app/data/memory.db")
rows = c.execute(
    "select trust_score, interaction_count from relationships "
    "where other_participant_id='mt-chat-004-user'"
).fetchall()
print("baseline relationship rows:", rows or "none (clean)")
PY
```

**Expected Result**: No prior relationship for `mt-chat-004-user` (or a recorded baseline count
to subtract later).

**Verification**:
- [ ] Baseline clean (no row) or baseline `interaction_count` noted

---

### Step 2: Send One Conversation (5 turns, one session)

**Action**: Send 5 turns reusing one `chat_session_id` — this is **one** conversation:

```bash
USER_ID="mt-chat-004-user"
R=$(curl -s -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"Hello! I am starting a new project today.\", \"user_id\": \"$USER_ID\"}")
echo "$R"; SID=$(echo "$R" | jq -r .chat_session_id)
for i in 2 3 4 5; do
  curl -s -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
    -H "Content-Type: application/json" \
    -d "{\"message\": \"Test message number $i for the relationship test.\", \"user_id\": \"$USER_ID\", \"chat_session_id\": \"$SID\"}" >/dev/null
done
echo "5 turns sent"
```

**Expected Result**: All 5 requests HTTP 200 (`reply_status: "ok"`, or `"empty"` occasionally —
acceptable). The interaction is still **open** — no relationship row yet.

**Verification**:
- [ ] 5 responses HTTP 200
- [ ] (Optional) re-run Step 1's query — still no relationship row (interaction open)

---

### Step 3: Close the Conversation (idle-gap + nudge)

**Action**: Wait past the timeout, then nudge to materialize the close:

```bash
sleep 20
curl -s -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"(nudge)\", \"user_id\": \"$USER_ID\"}" >/dev/null
sleep 3   # let the Phase-2 finalize (summary + record_interaction) land
```

> The nudge turn closes the 5-turn conversation **and** opens a new (1-turn) interaction in the
> same DM scope. We measure the *closed* conversation.

**Verification**:
- [ ] `agent_interactions_closed_total{agent_id="ember-owl"}` increased by 1 (see MT-CHAT-003 Step 3)

---

### Step 4: Verify ONE Interaction Recorded (not five)

**Action**:

```bash
docker exec -i persatrix-agent-ember-owl-1 python - <<'PY'
import sqlite3
c = sqlite3.connect("/app/data/memory.db")
u = "mt-chat-004-user"
rel = c.execute(
    "select trust_score, interaction_count, other_participant_type, last_interaction_at "
    "from relationships where other_participant_id=?", (u,)
).fetchone()
print("relationship:", rel)
ix = c.execute(
    "select interaction_type, substr(outcome,1,50) from interactions "
    "where other_participant_id=?", (u,)
).fetchall()
print("interaction rows:", len(ix))
for r in ix:
    print("  ", r)
PY
```

**Expected Result**: A relationship row exists with **`interaction_count == 1`** (the *one*
closed conversation — NOT 5), `other_participant_type == "user"`, and a recent
`last_interaction_at`. The `interactions` table shows **one** row with
`interaction_type == "conversation"` and an `outcome` carrying the conversation summary.

**Verification**:
- [ ] `interaction_count` increased by exactly **1** over the Step 1 baseline
- [ ] `other_participant_type` is `"user"`
- [ ] Exactly one new `interactions` row, `interaction_type == "conversation"`
- [ ] That interaction's `outcome` is non-empty (the summary)

---

### Step 5: Verify Trust Score Unchanged (default 0.5)

**Action**: Read `trust_score` from the Step 4 output.

**Expected Result**: `trust_score == 0.5`. `record_interaction` (the close-path call) records the
interaction but does **not** adjust trust — trust changes only via explicit `update_trust()` /
`apply_decay()`. RFC 0020's outcome→trust delta is deferred, so `0.5` is correct.

**Verification**:
- [ ] `trust_score` is `0.5` (default neutral)

---

### Step 6: (Optional) A Second Conversation → `interaction_count == 2`

**Action**: To confirm the count tracks *closed conversations*, run a second session for the
same user (new `chat_session_id`), then close it (Step 3). Re-query Step 4.

**Expected Result**: `interaction_count == 2` — one per closed conversation, regardless of how
many turns each contained.

**Verification**:
- [ ] After a second closed conversation, `interaction_count == 2`

---

### Step 7: Restore Config

Remove the temporary `interaction_idle_timeout_sec` line, `make validate`, restart the persona.

**Verification**:
- [ ] `config/agents.yaml` restored (`git diff` clean); `make validate` exits 0

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Baseline recorded (no prior relationship, or count noted) | ☐ |
| 2 | 5 turns return HTTP 200; no relationship row yet (interaction open) | ☐ |
| 3 | Idle-gap + nudge closes the conversation | ☐ |
| 4 | `interaction_count` += 1 (one conversation), type `"conversation"`, participant `"user"` | ☐ |
| 5 | Trust score is `0.5` (default neutral) | ☐ |
| 6 | (Optional) second closed conversation → count 2 | ☐ |
| 7 | Config restored | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Interaction Still Open
If you query before the conversation closes (no nudge after the timeout), there is **no**
relationship row yet and `interaction_count == 0`. The bump happens only at close — this is the
RFC 0020 behaviour (the v1.0 recipe's per-turn assumption was wrong).

### Edge Case 2: Summarisation Failed at Close
If the Phase-2 summary fails (timeout / empty / janitor backfill to
`[interaction summary unavailable]`), `record_closed_interaction` is **skipped** — the relationship
bump and auto-reflect tick do not fire for that interaction (`interaction_count` stays unchanged).
A successful close with a real or unavailable-but-finalized summary records the interaction.

### Edge Case 3: Self-DM Scope
A `dm:<id>:<id>` self-conversation has no peer, so no relationship row is created (peer extraction
returns none). Human chat (`dm:<agent>:<user>`) always has a distinct peer and records normally.

---

## API Key Requirement

| Step | Requires `ANTHROPIC_API_KEY` |
|------|------------------------------|
| 1 | No (direct DB query) |
| 2 | Yes (agent calls LLM) |
| 3 | Yes (nudge turn + Phase-2 close summary) |
| 4 | No (direct DB query) |
| 5 | No (read from Step 4) |
| 6 | Yes (second conversation) |
| 7 | No (config restore) |
