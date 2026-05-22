# Manual Test MT-CHAT-003: Chat Session Continuity Across Agent Restart

**Test ID**: `MT-CHAT-003`
**Feature Area**: Chat
**Version**: 1.1
**Created**: 2026-04-20
**Last Updated**: 2026-05-22
**Status**: Active

---

## Overview

**Purpose**: Verify that a chat conversation is persisted to episodic memory and survives an
agent restart. A new chat session with the same `user_id` should be able to reference prior
conversation content via the agent's recalled memory.

**Scope**: The RFC 0020 interaction lifecycle — a conversation is persisted as **one episodic
episode written at interaction *close***, not per turn. Close, the two-phase summary write,
cross-restart persistence (SQLite named volume), and post-restart recall.

**Out of Scope**: REST endpoint shape validation (MT-CHAT-001); CLI REPL mechanics (MT-CHAT-002);
relationship memory / trust evolution (MT-CHAT-004).

> **v1.1 rewrite (RFC 0020 interaction-close model).** The v1.0 recipe queried episodic memory
> *immediately after sending turns* and expected an episode to exist. Under [RFC 0020](../rfcs/0020-interaction-lifecycle.md)
> episodic episodes are written **at interaction close**, not per turn — so a query mid-conversation
> finds nothing. This version closes the interaction first, then verifies.

---

## How an interaction closes (read first)

Per [RFC 0020](../rfcs/0020-interaction-lifecycle.md), a conversation is an *interaction*: a
bounded sequence of turns in one scope (for human chat, a `dm:<agent>:<user>` scope). One
**closed** interaction produces exactly one episode (the conversation summary).

**There is no explicit "end chat" endpoint on the live stack.** The gRPC/REST chat path never
emits `chat_end`/`session_end` metadata, so an interaction closes only via the **idle-gap
timeout** (`memory.interaction_idle_timeout_sec`, default **600 s**;
[`agents/memory/boundary_detectors.py`](../../agents/memory/boundary_detectors.py)). The close is
materialized **lazily** — `idle_check` runs when the *next* event arrives (or the on-tick
janitor fires). A `timers: []` persona never ticks, so on the live stack **the close is
triggered by sending one more "nudge" turn after the timeout elapses**.

The write is **two-phase** ([`agents/persona_runtime/episode_routing.py`](../../agents/persona_runtime/episode_routing.py),
[`summarize_close.py`](../../agents/persona_runtime/summarize_close.py)): a `closing` episode row
with `summary = '[summary pending]'` is inserted synchronously, then a background task replaces
the summary with the LLM text. A query immediately after close may briefly show `[summary pending]`
— re-query.

---

## Related Documentation

**Feature Documentation**:
- [docs/rfcs/0020-interaction-lifecycle.md](../rfcs/0020-interaction-lifecycle.md) — interaction model.
- [agents/memory/interactions.py](../../agents/memory/interactions.py) — `InteractionTracker`, open/close.
- [agents/memory/boundary_detectors.py](../../agents/memory/boundary_detectors.py) — idle-gap close + `interaction_idle_timeout_sec`.
- [agents/persona_runtime/episode_routing.py](../../agents/persona_runtime/episode_routing.py) — `_persist_closed_interaction` (Phase 1).
- [agents/persona_runtime/summarize_close.py](../../agents/persona_runtime/summarize_close.py) — `finalize_closed_interaction` (Phase 2).
- [agents/memory/episodic.py](../../agents/memory/episodic.py) — episodic memory store.

**Related Automated Tests**:
- Integration: `tests/integration/test_summarize_on_close.py`, `test_summarize_on_close_phases.py`, `test_interaction_multi_turn.py`.

**Related Manual Tests**:
- [MT-CHAT-004](MT-CHAT-004.md) — relationship interaction recorded at close.

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

- ☐ Orchestrator + at least one persona agent (e.g. `ember-owl`) running and healthy
  (`GET /api/v1/agents` lists it `healthy`).
- ☐ Config files valid: `make validate` exits 0.

### Test Data — shorten the idle timeout

The default 600 s idle timeout is impractical for a manual run. Lower it for the persona under
test by adding `interaction_idle_timeout_sec` to its `memory:` block in `config/agents.yaml`:

```yaml
    memory:
      db_path: "data/memory.db"
      interaction_idle_timeout_sec: 15   # TEMPORARY for MT-CHAT-003 — restore after (default 600)
      # ... existing notes / facts / procedural_memory blocks unchanged ...
```

Run `make validate`, then restart the persona so it reloads config:

```bash
# Docker stack:
docker compose restart agent-ember-owl
# Local run: stop and re-run `make run-agent`.
```

> **Docker + `ANTHROPIC_API_KEY` note**: if an empty `ANTHROPIC_API_KEY` is exported in your
> shell, `docker compose` fails interpolation. Either unset it (so Compose reads `.env`) or use
> the plain container name: `docker restart persatrix-agent-ember-owl-1`.

Use `USER_ID="mt-chat-003-user"` throughout.

---

## Test Procedure

### Step 1: Establish a Chat Conversation

**Action**: Send 2–3 turns on a distinctive topic, reusing the `chat_session_id` from turn 1:

```bash
USER_ID="mt-chat-003-user"
R=$(curl -s -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"I have a pet turtle named Archimedes who loves swimming in circles.\", \"user_id\": \"$USER_ID\"}")
echo "$R"; SID=$(echo "$R" | jq -r .chat_session_id)

curl -s -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"Archimedes is 12 and his shell has a small crack on the left side.\", \"user_id\": \"$USER_ID\", \"chat_session_id\": \"$SID\"}"

curl -s -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"What should I do about the crack in his shell?\", \"user_id\": \"$USER_ID\", \"chat_session_id\": \"$SID\"}"
```

**Expected Result**: All requests HTTP 200 with `reply_status: "ok"`.

**Verification**:
- [ ] All responses HTTP 200 with `reply_status: "ok"`
- [ ] Replies acknowledge the turtle topic; note the `chat_session_id`

---

### Step 2: Confirm Nothing Is Persisted Yet (interaction still open)

**Action**: Query the agent's episodic store *before* closing the interaction:

```bash
docker exec -i persatrix-agent-ember-owl-1 python - <<'PY'
import sqlite3
c = sqlite3.connect("/app/data/memory.db")
rows = c.execute(
    "select count(*) from episodes where summary != '[summary pending]'"
).fetchone()[0]
print("finalized episodes so far:", rows)
PY
```

**Expected Result**: The turtle conversation is **not yet** an episode — the interaction is
still open (this is the RFC 0020 behaviour the v1.0 recipe got wrong).

**Verification**:
- [ ] No finalized episode for this conversation exists yet (open interactions are in-memory only)

---

### Step 3: Close the Interaction (idle-gap timeout + nudge)

**Action**: Wait past the idle timeout, then send **one nudge turn** to materialize the close
of the turtle interaction (a `timers: []` persona will not self-close):

```bash
sleep 20                      # > interaction_idle_timeout_sec (15)
curl -s -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"(nudge)\", \"user_id\": \"$USER_ID\"}" >/dev/null
sleep 3                       # let the Phase-2 background summary land
```

Confirm a close fired via the OTEL counter:

```bash
curl -s "http://127.0.0.1:9091/api/v1/query?query=agent_interactions_closed_total" \
  | jq '.data.result[] | select(.metric.agent_id=="ember-owl") | .value[1]'
```

**Expected Result**: `agent_interactions_closed_total{agent_id="ember-owl"}` is ≥ 1 (and
`agent_interactions_closed_by_idle_gap_total` rises) — the turtle interaction closed by idle gap.

**Verification**:
- [ ] `agent_interactions_closed_total` for ember-owl ≥ 1 after the nudge
- [ ] Close reason is idle-gap (`agent_interactions_closed_by_idle_gap_total` increments)

---

### Step 4: Verify the Conversation Episode Was Written at Close

**Action**:

```bash
docker exec -i persatrix-agent-ember-owl-1 python - <<'PY'
import sqlite3
c = sqlite3.connect("/app/data/memory.db")
rows = c.execute(
    "select interaction_id, turn_count, scope, closed_at, substr(summary,1,80) "
    "from episodes where summary != '[summary pending]' "
    "order by closed_at desc limit 5"
).fetchall()
print("finalized episodes:", len(rows))
for r in rows:
    print(" ", r)
PY
```

**Expected Result**: At least one episode with `scope` like `dm:ember-owl:mt-chat-003-user`,
`closed_at` set, `turn_count` ≈ the turns sent, and a `summary` that references the conversation
(turtle / Archimedes / shell). If the summary still shows `[summary pending]`, wait a few seconds
and re-query (Phase-2 is async).

**Verification**:
- [ ] ≥ 1 finalized episode for the `dm:ember-owl:mt-chat-003-user` scope
- [ ] `turn_count` reflects the conversation; `closed_at` is set
- [ ] `summary` is non-pending and references the topic

---

### Step 5: Restart the Agent and Confirm Persistence

**Action**: Restart the agent and confirm the episode survives:

```bash
docker compose restart agent-ember-owl   # or: docker restart persatrix-agent-ember-owl-1
# wait for healthy, then re-query:
docker exec -i persatrix-agent-ember-owl-1 python - <<'PY'
import sqlite3
c = sqlite3.connect("/app/data/memory.db")
print("episodes after restart:",
      c.execute("select count(*) from episodes where summary != '[summary pending]'").fetchone()[0])
PY
```

**Expected Result**: The agent re-registers `healthy`; the episode count is unchanged (the
SQLite named volume persists across restart).

**Verification**:
- [ ] Agent re-registers as healthy
- [ ] The closed-interaction episode still present after restart

---

### Step 6: Post-Restart Recall (user-facing proof)

**Action**: Send a new chat turn (new session) asking the agent to recall the prior topic:

```bash
curl -s -X POST http://localhost:8080/api/v1/agents/ember-owl/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"Do you remember the pet I told you about? What was its name?\", \"user_id\": \"mt-chat-003-user\"}" \
  | jq '{reply_status, reply}'
```

**Expected Result**: HTTP 200, `reply_status: "ok"`, and the reply references the pre-restart
conversation (Archimedes / turtle / shell crack), recalled from the persisted episode.

> **Acceptable partial pass**: if the LLM reply does not name "Archimedes" but Step 4 confirmed
> the episode persisted, the test passes for the *persistence* requirement. LLM recall quality
> is model-dependent and not the primary target.

**Verification**:
- [ ] HTTP 200, `reply_status: "ok"`
- [ ] Reply references the pre-restart conversation (or persistence confirmed in Step 4)

---

### Step 7: Restore Config

**Action**: Remove the temporary `interaction_idle_timeout_sec` line from `config/agents.yaml`
(restore the 600 s default), `make validate`, and restart the persona.

**Verification**:
- [ ] `config/agents.yaml` restored (`git diff` clean); `make validate` exits 0
- [ ] Persona restarts cleanly

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | 2–3 chat turns return HTTP 200 / `reply_status: ok` | ☐ |
| 2 | No finalized episode yet (interaction open) | ☐ |
| 3 | Idle-gap timeout + nudge closes the interaction (`agent_interactions_closed_total` ≥ 1) | ☐ |
| 4 | One episode written at close, scope `dm:…`, summary references the topic | ☐ |
| 5 | Episode survives agent restart | ☐ |
| 6 | Post-restart recall references the prior conversation (or persistence confirmed) | ☐ |
| 7 | Config restored | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Agent Database Deleted Before Restart
Delete the persona's data volume, then restart and recall. Expected: no episode; the reply does
not reference the turtle — confirming persistence is on disk, not in-process state.

### Edge Case 2: Query During the Two-Phase Window
If Step 4 is run immediately after close, the episode may show `summary = '[summary pending]'`
(Phase-1 row before the Phase-2 background summary lands). Re-query after a few seconds; the
summary upgrades to the LLM text (or to `[interaction summary unavailable]` if summarisation
failed — still a valid closed episode).

### Edge Case 3: No Nudge Sent
If the conversation is left idle past the timeout but **no** subsequent event arrives and the
persona has `timers: []`, the close is never materialized (the episode stays unwritten). This is
expected — the idle close is lazy. Send any further event to trigger it.

---

## API Key Requirement

| Step | Requires `ANTHROPIC_API_KEY` |
|------|------------------------------|
| 1 | Yes (agent calls LLM) |
| 2 | No (direct DB query) |
| 3 | Yes (nudge turn) + the close summary (Phase-2) calls the LLM |
| 4 | No (direct DB query) |
| 5 | No (restart + DB query) |
| 6 | Yes (agent calls LLM) |
| 7 | No (config restore) |
