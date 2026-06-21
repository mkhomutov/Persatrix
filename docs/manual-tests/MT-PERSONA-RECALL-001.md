# Manual Test MT-PERSONA-RECALL-001: Verbatim recall is scoped to where the persona was present

**Test ID**: `MT-PERSONA-RECALL-001`
**Feature Area**: Channels / Persona memory (verbatim message recall — RFC 0036 + RFC 0035 ledger)
**Version**: 1.0
**Created**: 2026-06-21
**Last Updated**: 2026-06-21
**Status**: Active

---

## Overview

**Purpose**: Verify the v0.3.9 headline promise end-to-end with a real
persona — **personas quote each other accurately, and a human can search the
verbatim record of what was said, scoped to the channels each persona was
present for**. A persona added → removed → re-added to a group channel recalls
the verbatim text of **both** of its membership stints, and of **neither** the
pre-join period nor the removal gap. The access rule is enforced server-side
in SQL ([RFC 0036 §C](../rfcs/0036-persona-message-recall.md#c-the-scoped-search-query)):
a `messages_fts` FTS5 index joined against the [RFC 0035](../rfcs/0035-channel-membership-interval-ledger.md)
`membership_intervals` ledger, so the persona runtime never sees a message it
was not entitled to. The visible contract: *what a persona can recall is
exactly what it was present for — not more (a gap is unreachable), not less
(both stints are reachable).*

The deterministic half of this arc is pinned by the automated store- and
endpoint-level suites (see **Related Automated Tests**); this MT covers what
automation cannot — whether a **real persona**, given the
`recall_channel_messages` tool, actually reaches for it to quote a prior
message and is held to the same scope the raw query enforces.

**Scope**: a fresh REST-created group channel (`group:mt-recall-001`) so the
membership add/remove is clean and not subject to the config-declared
boot reconcile; `ember-owl` (the demo persona granted `channels:recall` in
[`config/agents.yaml`](../../config/agents.yaml)) as the recalling
`participant`; a human `alex` as a `respond: never` member driving the
timeline. Both recall paths are exercised: the deterministic
`POST …/recall` endpoint (no LLM — proves the scope) and the persona tool
(LLM — proves the persona uses it and is bound to the same scope). Plus the
RFC 0035 Phase 2 membership-history endpoint as the audit cross-check, and a
verification that every recall emits one server-side `channel.recall` audit
event recording the **count, not the content**.

**Out of scope**:
- **Live epoch isolation.** The §OQ-6 `epoch_id` hard filter is a correctness
  requirement, but the channel store is **not epoch-partitioned in
  production**: the publish path never stamps a non-`live` epoch on a persisted
  message (the override rides the gRPC dispatch rail, not the row), so every
  `messages.epoch_id` is `live` and a cross-epoch message **cannot be produced
  through the real publish path**. The filter is therefore a forward-looking
  defensive guard, pinned deterministically by
  `TestRecallEndpoint_RealPublishPath_ExplicitEpochUnreachable` and
  `TestRecallMessages_Epoch_HardFilter` — not a live MT lever. (Same posture
  as the synthetic-only governance levers: where production cannot naturally
  produce the input, the deterministic test is the acceptance, and this MT
  records why rather than faking it.)
- **Cross-session recall in a single run.** That `session_id` is *not* filtered
  (recall spans sessions within the epoch) is pinned by
  `TestRecallMessages_SpansSessions`; the optional Step 7 below demonstrates it
  live by recalling stint-1 content *after a stack restart* (a restart opens a
  new session), but the deterministic test is the acceptance.
- The episodic *summary* tier ([persona-agents guide §2](../guides/persona-agents.md#2-the-three-memory-tiers)),
  semantic/vector recall, and cross-persona recall — all
  [out of scope for v0.3.9](../v0.3.9-plan.md#why-this-plan-exists).

---

## Related Documentation

- [RFC 0036 — Persona Verbatim Message Recall](../rfcs/0036-persona-message-recall.md) — §C scoped query, §E the tool, §F sanitization, §G window filter, §Security audit
- [RFC 0035 — Channel Membership Interval Ledger](../rfcs/0035-channel-membership-interval-ledger.md) — the ledger recall scoping joins; §D the backfill gap (a known recall limitation)
- [v0.3.9 master plan](../v0.3.9-plan.md) — the release contract this MT is the acceptance for; the §OQ-6 epoch-filter / session-span lock
- [persona-agents guide §2](../guides/persona-agents.md#2-the-three-memory-tiers) — verbatim recall vs. the summary tiers

**Related Automated Tests**:
- [`sqlite_search_test.go`](../../internal/channels/sqlite_search_test.go) — `TestRecallMessages_Scope_JoinLeaveRejoin` (both stints reachable, pre-join + gap excluded), `TestRecallMessages_Epoch_HardFilter`, `TestRecallMessages_SpansSessions`, `TestRecallMessages_Narrowing` / `_Ranking` / `_MatchSafety` / `_NonLatinQuery` / `_LimitClamp` / `_Retention_DeletedMessageGone`
- [`persona_recall_handlers_test.go`](../../internal/server/persona_recall_handlers_test.go) — `TestRecallEndpoint_JoinLeaveRejoin_BothStintsGapExcluded`, `TestRecallEndpoint_RealPublishPath_Recallable`, `TestRecallEndpoint_RealPublishPath_ExplicitEpochUnreachable`, `TestRecallEndpoint_EmitsAuditEvent_CountNotContent`, `TestRecallEndpoint_LimitClampedServerSide`
- [`test_recall_tool.py`](../../tests/unit/python/test_recall_tool.py) — closure-bound `agent_id`, `channels:recall` denial, the `<|user_message|>` round-trip-inert escape
- [`channel_history_scoped_test.go`](../../internal/server/channel_history_scoped_test.go) — the §G window-filter endpoint (`?as_participant`): `TestHistoryEndpoint_AsParticipant_ScopesToMembership` / `_NonMemberEmpty`
- [`sqlite_history_scoped_test.go`](../../internal/channels/sqlite_history_scoped_test.go) — the store-level §G filter; `TestGetHistoryScoped_CurrentMemberMatchesUnscopedTail` (no-op for current members)

---

## Preconditions

- ☐ Valid API key — the persona replies and the tool-path recall in Step 5 are
  real LLM calls. (Steps 3 and 6 — the `POST …/recall` endpoint and the
  membership-history endpoint — are deterministic SQL and need no key.)
- ☐ The demo stack is up with the UI enabled (`--enable-ui`), and `./bin/persatrix`
  is on `PATH` pointing at it (`PERSATRIX_API` / default `http://127.0.0.1:8080`).
- ☐ Clean state (`make reset` or a fresh `PERSATRIX_EPOCH`) so the channel
  store opens, migrates **v8 → v9 → v10**, and starts with no `group:mt-recall-001`.
- ☐ `ember-owl` carries `permissions.channels.recall: true` in
  [`config/agents.yaml`](../../config/agents.yaml) (ships granted).

```bash
make reset
ENABLE_UI=1 docker compose up --build
```

Throughout, `$API` is the base URL (default `http://127.0.0.1:8080`) and `$CH`
is `group:mt-recall-001`. Note the create body posts the **bare** `name`
(`mt-recall-001`); the server canonicalises it to the `group:`-prefixed id
(`canonicalID := "group:" + name`), so do **not** pre-prefix the `name` or the
channel lands at `group:group:mt-recall-001` and every later `$CH` call 404s.

---

## Test Procedure

### Step 1: Create the channel and lay down stint 1

Create a fresh group channel with `ember-owl` as a `participant` and `alex` as
a `respond: never` human driver, then seed a memorable fact while `ember-owl`
is present.

```bash
curl -sS -X POST "$API/api/v1/channels" -H 'Content-Type: application/json' -d '{
  "name": "mt-recall-001",
  "description": "MT-PERSONA-RECALL-001 scoped recall fixture",
  "members": [
    {"id": "ember-owl", "respond": "participant"},
    {"id": "alex",      "respond": "never"}
  ]
}'

./bin/persatrix channel send "$CH" \
  "@ember-owl decision: the production deploy window is Thursday 14:00 UTC. Please ack." --as alex
```

**Expected**:
- The channel is created with both members.
- `ember-owl` replies in-channel acknowledging the Thursday 14:00 window — its
  reply and `alex`'s message are **stint-1** content (both fall inside
  `ember-owl`'s first open interval).

**Verification**:
- [ ] `ember-owl` replied; `./bin/persatrix channel history "$CH" --json` shows
  `alex`'s decision message and `ember-owl`'s ack.

### Step 2: Remove `ember-owl`, then post a gap message

```bash
curl -sS -X DELETE "$API/api/v1/channels/$CH/members/ember-owl" -i   # → 204

./bin/persatrix channel send "$CH" \
  "While Ember is away: if the deploy breaks, the emergency rollback key is in vault slot 7." --as alex
```

**Expected**:
- The DELETE returns `204`; `ember-owl`'s first interval is now **closed**.
- The vault-slot-7 message is published while `ember-owl` is **not** a member —
  it is **gap** content, inside no interval of `ember-owl`'s.

**Verification**:
- [ ] DELETE returned 204; the gap message is in `channel history` but
  `ember-owl` did **not** reply to it (it is not a member).

### Step 3: Re-add `ember-owl`, lay down stint 2, then recall (deterministic, no LLM)

```bash
curl -sS -X POST "$API/api/v1/channels/$CH/members" -H 'Content-Type: application/json' \
  -d '{"id": "ember-owl", "respond": "participant"}'   # → 204, opens stint 2

./bin/persatrix channel send "$CH" \
  "@ember-owl welcome back — remind me of the deploy window and confirm staffing." --as alex

# Recall the single shared term "deploy" — it appears in BOTH stints AND in the
# gap message, so the gap's absence proves SCOPE (not the query text) decides.
# (FTS5 MATCH ANDs every query token, so a multi-term query spanning words that
# never co-occur in one message would return nothing — a single shared term is
# what isolates the membership filter as the only variable.)
curl -sS -X POST "$API/api/v1/personas/ember-owl/recall" \
  -H 'Content-Type: application/json' \
  -d '{"query": "deploy", "channel_id": "group:mt-recall-001", "limit": 20}' \
  | jq -r '.messages[] | "\(.sender)\t\(.content)"'
```

**Expected**:
- The re-add returns `204` and opens a **second** interval (`ember-owl` now has
  one closed + one open).
- The recall payload contains the **stint-1** deploy decision (and `ember-owl`'s
  stint-1 ack), and the **stint-2** turns — but **never** the *vault slot 7*
  gap message, **even though that gap message also contains the query term
  `deploy`**. The membership `EXISTS` clause, not the query text, decides
  reachability.

**Verification**:
- [ ] The recalled set includes the Thursday-14:00 decision and stint-2 turns.
- [ ] The *vault slot 7* gap message is **absent** from the recall payload
  (this is the load-bearing assertion).

### Step 4: A gap-only query returns nothing

```bash
curl -sS -X POST "$API/api/v1/personas/ember-owl/recall" \
  -H 'Content-Type: application/json' \
  -d '{"query": "emergency rollback key slot", "channel_id": "group:mt-recall-001"}' \
  | jq '.messages | length'
```

**Expected**: `0` — the only message matching those terms is the gap message,
which is out of `ember-owl`'s scope.

**Verification**:
- [ ] The gap-only query returns an empty `messages` array.

### Step 5: The persona reaches for the tool — and is held to the same scope

Ask `ember-owl` to recall, in natural language, both an in-scope fact and the
out-of-scope gap fact.

```bash
./bin/persatrix channel send "$CH" \
  "@ember-owl what exactly was the deploy window we agreed on? Quote it." --as alex
# …then, separately:
./bin/persatrix channel send "$CH" \
  "@ember-owl do you have any record of where the emergency rollback key is?" --as alex
```

**Expected**:
- For the deploy-window ask, `ember-owl` invokes `recall_channel_messages`
  (visible in the server logs as a `channel.recall` audit event and a tool
  call) and answers with the **verbatim** Thursday 14:00 UTC window.
- For the rollback-key ask, `ember-owl` **cannot** produce *vault slot 7* — the
  message is outside its membership scope, so the tool returns nothing for it
  and the persona honestly reports it has no record (it must **not**
  hallucinate the slot number). The scope participant is closure-bound to
  `ember-owl`; no tool argument can widen it.

**Verification**:
- [ ] `ember-owl` quotes the correct deploy window via recall.
- [ ] `ember-owl` does **not** surface the vault-slot-7 gap fact.

### Step 6: Membership history + audit cross-check

```bash
# RFC 0035 Phase 2 — the operator inspection endpoint shows the two stints:
curl -sS "$API/api/v1/channels/$CH/members/ember-owl/history" | jq

# The recall calls above each emitted a server-side audit event (count, not content):
docker compose logs orchestrator | grep -i 'channel.recall' | tail -5
```

**Expected**:
- The history endpoint returns **two** intervals for `ember-owl`: one closed
  `[t0, t1)` (stint 1) and one open `[t2, NULL)` (stint 2), non-overlapping.
- Each recall (Steps 3–5) produced exactly one `channel.recall` audit event
  recording the persona, query, narrowing params, and result **count** — and
  **not** the recalled message content.

**Verification**:
- [ ] Two non-overlapping intervals (one closed, one open).
- [ ] One audit event per recall; the recalled content never appears in the log.

### Step 7 (optional): Cross-session recall survives a restart

```bash
docker compose restart orchestrator   # a restart opens a NEW session_id
# After the persona reconnects, repeat the Step 3 recall:
curl -sS -X POST "$API/api/v1/personas/ember-owl/recall" \
  -H 'Content-Type: application/json' \
  -d '{"query": "deploy window", "channel_id": "group:mt-recall-001"}' \
  | jq '.messages | length'
```

**Expected**: the stint-1 deploy decision (authored before the restart, under
the prior session) is **still recalled** — recall spans sessions within the
epoch (`session_id` is not filtered; the membership interval is the boundary).

**Verification**:
- [ ] Stint-1 content authored under the prior session is still recalled after
  the restart.

---

## Pass / Fail Summary

| # | Check | Pass |
|---|-------|------|
| 1 | Channel created; `ember-owl` replies — stint-1 content laid down | ☐ |
| 2 | DELETE 204 closes stint 1; gap message posted while `ember-owl` absent | ☐ |
| 3 | Re-add 204 opens stint 2; recall returns both stints, **excludes the gap** | ☐ |
| 4 | A gap-only query returns an empty result | ☐ |
| 5 | The persona recalls the in-scope fact via the tool; **cannot** surface the gap fact | ☐ |
| 6 | History shows two non-overlapping intervals; one `channel.recall` audit event per recall (count, not content) | ☐ |
| 7 | (optional) Stint-1 content is still recalled after a restart (cross-session) | ☐ |

**Overall**: ☐ PASS ☐ FAIL

A FAIL on check 3 or 5 (the gap is reachable) is a **data-exposure** defect and
blocks the v0.3.9 cut; a FAIL on the gap being *unreachable while a stint is
also unreachable* is a data-suppression defect — both trace to ledger /
`EXISTS`-clause correctness and must be triaged against
[`sqlite_search_test.go`](../../internal/channels/sqlite_search_test.go) before
release.

---

## Notes

- **Why a fresh REST channel, not the demo `planning` channel.** Removing a
  config-declared member from `planning` would be re-added by the boot
  reconcile (RFC 0011 §B), reopening a fresh interval and muddying the
  two-stint shape. A store-created channel is not reconciled, so the add →
  remove → re-add cycle is exactly two clean intervals.
- **Epoch isolation is deterministic-only by design** — see **Out of scope**.
  Do not attempt to fake a cross-epoch message through the publish path; the
  production store is single-epoch (`live`) and the guard is pinned by
  `TestRecallEndpoint_RealPublishPath_ExplicitEpochUnreachable`.
- Live execution of this MT on the release tip is a
  [v0.3.9 release-prep Phase 3](../v0.3.9-plan.md#phase-3--v039-release-prep-execution)
  deliverable; this file is the authored acceptance spec (RFC 0036 PR 6).
