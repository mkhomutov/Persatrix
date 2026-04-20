# Manual Test MT-CHAT-003: Chat Session Continuity Across Agent Restart

**Test ID**: `MT-CHAT-003`
**Feature Area**: Chat
**Version**: 1.0
**Created**: 2026-04-20
**Last Updated**: 2026-04-20
**Status**: Active

---

## Overview

**Purpose**: Verify that chat messages are persisted in episodic memory so that a conversation
can survive an agent restart. After restarting the agent process, a new chat session with the
same `--user` should be able to reference prior conversation content via the agent's memory.

**Scope**: Episodic memory persistence (SQLite), conversation recall after agent restart,
`UserStore` persistence.

**Out of Scope**: REST endpoint shape validation (MT-CHAT-001); CLI REPL mechanics (MT-CHAT-002);
relationship memory trust evolution (MT-CHAT-004).

---

## Related Documentation

**Feature Documentation**:
- [agents/memory/episodic.py](../../agents/memory/episodic.py) — episodic memory (SQLite)
- [agents/participant.py](../../agents/participant.py) — `UserStore` persistence
- [agents/server_servicers.py](../../agents/server_servicers.py) — `SendChatMessage` servicer
- [agents/persona_runtime/](../../agents/persona_runtime/) — persona event handling

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
- Rust CLI built: `make build-cli`
- `curl` available in PATH

### Application State

- ☐ Orchestrator running: `make run`
- ☐ At least one persona agent registered and healthy (e.g. `ember-owl`): `make run-agent`
- ☐ Config files valid: `make validate`
- ☐ `ANTHROPIC_API_KEY` set in environment
- ☐ Clean memory state: remove any pre-existing agent memory databases if needed to isolate this
  test (check `data/` or the agent's configured data directory for `.db` files)

### Test Data

No external fixtures. All interaction is via `curl` and CLI commands.

---

## Test Procedure

### Step 1: Establish a Chat Conversation (Pre-Restart)

**Action**: Send 2–3 chat messages with a distinctive topic so the agent stores them in episodic
memory. Use a consistent `user_id`:

```bash
# Message 1
curl -s -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "I have a pet turtle named Archimedes who loves swimming in circles.", "user_id": "mt-chat-003-user"}'

# Message 2 (use session_id from Message 1 response)
curl -s -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Archimedes is 12 years old and his shell has a small crack on the left side.", "user_id": "mt-chat-003-user", "session_id": "<SESSION_ID_FROM_MSG_1>"}'

# Message 3
curl -s -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What do you think I should do about the crack in his shell?", "user_id": "mt-chat-003-user", "session_id": "<SESSION_ID_FROM_MSG_1>"}'
```

**Expected Result**: All three requests return HTTP 200 with `reply_status: "ok"`. The agent
provides relevant replies.

**Verification**:
- [ ] All three responses are HTTP 200
- [ ] Each response has `reply_status: "ok"`
- [ ] The agent's replies acknowledge the turtle topic
- [ ] Note the `session_id` for reference

---

### Step 2: Verify Episodic Memory Contains the Conversation

**Action**: Use a Python script to query the agent's episodic memory for the conversation content:

```bash
python3 - <<'EOF'
import asyncio
from persatrix_agents.memory.episodic import EpisodicMemory

async def main():
    # Use the same agent_id and db_path that the running agent uses
    mem = EpisodicMemory("ember-owl")
    await mem.initialize()

    episodes = await mem.recall("turtle Archimedes", limit=5)
    print(f"Episodes found: {len(episodes)}")
    for ep in episodes:
        print(f"  [{ep.id[:8]}] {ep.summary[:80]}...")

    count = await mem.count_episodes()
    print(f"Total episodes for agent: {count}")

    await mem.close()

asyncio.run(main())
EOF
```

> **Note**: The `db_path` defaults to the agent's standard location. If the agent uses a custom
> path, adjust accordingly.

**Expected Result**: At least one episode related to the turtle conversation is found.

**Verification**:
- [ ] `Episodes found` is ≥ 1
- [ ] At least one episode summary references the conversation topic (turtle, Archimedes, shell)
- [ ] `Total episodes` is > 0

---

### Step 3: Stop and Restart the Agent

**Action**: Stop the running agent process (Ctrl-C in its terminal or `kill`), then restart it:

```bash
# Stop the agent (Ctrl-C in the agent terminal)
# Then restart:
make run-agent
```

Wait for the agent to register as healthy with the orchestrator (check orchestrator logs for
the agent health check passing).

**Expected Result**: The agent process restarts and re-registers with the orchestrator. The
SQLite database files in the agent's data directory are preserved (not deleted on restart).

**Verification**:
- [ ] Agent process starts without errors
- [ ] Orchestrator logs show the agent re-registering as healthy
- [ ] Agent's SQLite database files still exist on disk

---

### Step 4: Send a Recall Message (Post-Restart)

**Action**: Send a new chat message asking the agent to recall the pre-restart conversation:

```bash
curl -s -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Do you remember the pet I told you about? What was its name?", "user_id": "mt-chat-003-user"}'
```

> **Note**: This uses a new `session_id` (server-generated) since the original session was
> tied to the pre-restart process. The agent relies on episodic memory recall, not in-process
> session state.

**Expected Result**: HTTP 200. The agent's reply demonstrates recall of the turtle conversation
from episodic memory — it should reference "Archimedes", "turtle", or the shell crack topic.

**Verification**:
- [ ] HTTP status is `200`
- [ ] `reply_status` is `"ok"`
- [ ] Agent's reply references the pre-restart conversation (Archimedes, turtle, or shell)

> **Acceptable partial pass**: If the LLM reply does not explicitly name "Archimedes" but the
> episodic memory query in Step 2 confirmed persistence, the test passes for the *persistence*
> requirement. LLM recall quality is model-dependent and not the primary test target.

---

### Step 5: Verify UserStore Persistence

**Action**: Check that the user record survived the restart:

```bash
python3 - <<'EOF'
import asyncio
from persatrix_agents.participant import UserStore

async def main():
    store = UserStore("ember-owl")
    await store.initialize()

    user = await store.get("mt-chat-003-user")
    if user:
        print(f"User found: {user.participant_id}")
        print(f"  display_name: {user.display_name}")
        print(f"  participant_type: {user.participant_type}")
        print(f"  created_at: {user.created_at}")
        print(f"  last_seen_at: {user.last_seen_at}")
    else:
        print("User NOT found — persistence failed")

    await store.close()

asyncio.run(main())
EOF
```

**Expected Result**: The user record `mt-chat-003-user` is present in the store with a valid
`created_at` timestamp from Step 1 and a `last_seen_at` updated in Step 4.

**Verification**:
- [ ] `User found: mt-chat-003-user` printed
- [ ] `participant_type` is `"user"`
- [ ] `last_seen_at` is a recent timestamp (from Step 4)

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Three HTTP 200 responses with agent replies | ☐ |
| 2 | Episodic memory contains turtle conversation episodes | ☐ |
| 3 | Agent restarts and re-registers as healthy | ☐ |
| 4 | Post-restart reply references pre-restart conversation | ☐ |
| 5 | UserStore record persisted across restart | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Agent Database Deleted Before Restart

**Scenario**: Delete the agent's SQLite files, then restart. Send a recall message.

**Expected**: Agent has no memory of the conversation. Reply does not reference the turtle.
This confirms memory is stored in the database, not in-process state.

### Edge Case 2: Different User ID Post-Restart

**Scenario**: Send the recall message with a different `user_id` (e.g. `"different-user"`).

**Expected**: The agent may still recall the topic (episodic memory is per-agent, not per-user),
but relationship context will differ.

---

## API Key Requirement

| Step | Requires `ANTHROPIC_API_KEY` |
|------|------------------------------|
| 1 | Yes (agent calls LLM) |
| 2 | No (direct memory query) |
| 3 | No (process restart) |
| 4 | Yes (agent calls LLM) |
| 5 | No (direct store query) |
