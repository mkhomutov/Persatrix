# Manual Test MT-CHANNEL-GOV-002: Floor control — ordered, mutually-aware multi-persona replies

**Test ID**: `MT-CHANNEL-GOV-002`
**Feature Area**: Channels (conversation governance — RFC 0030 Layer 2.5)
**Version**: 1.0
**Created**: 2026-06-04
**Last Updated**: 2026-06-04
**Status**: Active

---

## Overview

**Purpose**: Verify the v0.3.6 user-facing promise of **floor control** (RFC 0030
Layer 2.5, the [floor-control amendment](../rfcs/0030-amendment-floor-control-speaker-serialization.md)):
when a single message lands in a group channel with two or more responders, the
personas now reply **one at a time, in a deterministic order, each having read
the prior speaker's reply** — instead of the pre-amendment behaviour where every
responder was fanned out concurrently and composed its reply blind to its peers
("the simultaneous shout"). The visible contract is **coherence**: later speakers
build on, defer to, or explicitly decline to repeat earlier speakers — including
at least one "nothing to add" when a point is already covered.

**Scope**: the default `planning` group channel (three personas: `ember-owl`,
`iron-fox`, `nova-sparrow`), one human prompt, and the serialized speaker round
that floor control drives in [`internal/channels/fanout.go`](../../internal/channels/fanout.go)
→ [`floorRound`](../../internal/channels/floor_control.go). Observed through the
embedded web console Channel-timeline panel (the PR-3 acceptance surface) with a
REST cross-check for the ordering/timing assertions.

**Out of Scope**: the registry mutual-exclusion and ordering-split unit
behaviour (covered by `internal/channels/floor_control_test.go`); the
one-at-a-time dispatch, deferred-fanout, and timeout-advance integration
behaviour with the flag forced on (covered by
`internal/channels/fanout_floor_test.go`). Telemetry counters/histograms for the
round are a fast-follow (floor-control PR 4) and are **not** asserted here.

---

## Related Documentation

- [RFC 0030 Amendment — Floor Control / Speaker Serialization](../rfcs/0030-amendment-floor-control-speaker-serialization.md) — design + locked decisions D1–D5
- [RFC 0030 floor-control PR plan](../rfcs/0030-amendment-floor-control-pr-plan.md) — this MT is PR 3's acceptance test
- [docs/guides/channels.md §7 "Floor control"](../guides/channels.md) — operator walkthrough this MT scripts
- [docs/guides/web-console.md](../guides/web-console.md) — the `--enable-ui` console surface

**Related Automated Tests**:
- `internal/channels/floor_control_test.go` — registry mutual-exclusion; responder ordering split (mentioned-first, stable tie-break)
- `internal/channels/fanout_floor_test.go` — one-at-a-time dispatch; mutual visibility across the round; deferred-fanout; timeout advance; DM/single-responder no-op
- `internal/channels/config_test.go` — `floor_control` tri-state: absent resolves ON for groups, explicit `false` opts out

---

## Preconditions

Same as [MT-CHANNEL-004 § Preconditions](MT-CHANNEL-004.md#preconditions)
(a valid `ANTHROPIC_API_KEY` in `.env` — the persona replies are real LLM
calls), **plus**:

- ☐ All three demo personas are up on the default compose stack:
  `agent-ember-owl`, `agent-iron-fox`, `agent-nova-sparrow` (declared in
  [config/agents.yaml](../../config/agents.yaml); the default `planning` channel
  in [config/channels.yaml](../../config/channels.yaml) wires all three).
- ☐ The default `config/channels.yaml` is unedited: the `planning` channel
  carries `floor_control: true` (the resolved group default, shown explicitly).
- ☐ The orchestrator is started with `--enable-ui` so the console is served at
  `http://localhost:8080/ui`.
- ☐ State is clean (`make reset` or a fresh `PERSATRIX_EPOCH`) so prior-run
  participants do not steer the conversation (see channels guide §10).

Bring the stack up:

```bash
make reset
ENABLE_UI=1 docker compose up --build   # or `make run-ui` for the local-binary path
```

Confirm floor control is active in the startup log — the orchestrator resolves
the flag per declared channel at boot:

```bash
docker compose logs orchestrator | grep -i "channels: subsystem ready"
```

---

## Test Procedure

### Step 1: Confirm the channel resolves floor control ON

**Action**: in the web console (`http://localhost:8080/ui`), open the **Channels**
panel and select **`group:planning`**. (Or via REST:
`Invoke-RestMethod http://127.0.0.1:8080/api/v1/channels/group:planning`.)

**Expected**:
- The channel exists with members `ember-owl`, `iron-fox`, `nova-sparrow`.
- `floor_control` is on for this channel (it is the resolved group default; the
  template sets it explicitly).

**Verification**:
- [ ] `group:planning` is listed with all three personas as members.

---

### Step 2: Post one human prompt that invites all three to weigh in

Post a single open question that the two `always` members (`iron-fox`,
`nova-sparrow`) will answer, and `@`-mention `ember-owl` so the advisor
(`when_mentioned`) joins the round as a third responder.

**Action** (console composer in the `group:planning` timeline, or CLI):

```bash
./bin/persatrix channel send planning \
  "We need to pick a datastore for the v0.4 event log: Postgres or SQLite? @ember-owl what's your read?" \
  --as operator --mention ember-owl
```

**Expected**:
- The publish succeeds (one human message in the timeline).
- Over the next ~10–90 s, **exactly one persona reply appears at a time** in the
  timeline — `ember-owl` first (it was mentioned → mentioned-first ordering),
  then `iron-fox`, then `nova-sparrow` (existing member order). The next reply
  does not begin composing until the previous one has posted.

**Verification**:
- [ ] Replies land **sequentially**, not in a single near-simultaneous burst.
- [ ] The first persona reply is from the mentioned member (`ember-owl`).

---

### Step 3: Confirm the replies are mutually aware (the coherence contract)

**Action**: read the three persona replies in order in the timeline.

**Expected** — the load-bearing assertion of this MT:
- The **second** speaker's reply references, agrees with, builds on, or
  explicitly disagrees with the **first** speaker's position (it was in the
  transcript the second persona composed against).
- The **third** speaker's reply is aware of **both** prior replies.
- **At least one** of the later speakers declines to restate a point already
  made — a "nothing to add to X's point" / brief concurrence / `DO_NOTHING`-style
  yield — rather than re-arguing the same case from scratch.

**Contrast (the pre-amendment baseline)**: before floor control, all three would
have answered the *original prompt only*, in parallel, with no reference to each
other — three independent "Postgres because…/SQLite because…" essays, often
mutually contradictory, none acknowledging the others. If you see that shape,
floor control is **not** engaged — re-check Step 1 and the startup log.

**Verification**:
- [ ] Speaker 2 explicitly references speaker 1's reply.
- [ ] Speaker 3 is aware of both prior replies.
- [ ] At least one later speaker yields / declines to repeat a covered point.

---

### Step 4: Confirm ordering and one-at-a-time timing via REST

**Action**: pull the channel history newest-first and inspect timestamps.

```pwsh
(Invoke-RestMethod `
  "http://127.0.0.1:8080/api/v1/channels/group:planning/messages?limit=10") `
  | Select-Object timestamp, sender_id, @{n='content';e={$_.content.Substring(0,[Math]::Min(60,$_.content.Length))}} `
  | Format-Table -Auto
```

**Expected**:
- One human message followed by three persona messages.
- Persona message timestamps are **non-overlapping and monotonically ordered**
  `ember-owl` → `iron-fox` → `nova-sparrow`; each reply's timestamp is after the
  previous reply's (the serialized round, not a concurrent burst).

**Verification**:
- [ ] Persona reply order is `ember-owl`, `iron-fox`, `nova-sparrow`.
- [ ] Each persona reply timestamp is later than the previous persona reply's.

---

### Step 5: Per-turn timeout advances a stalled round (optional)

A non-replying responder must not stall the round indefinitely — the loop
advances after `floor_turn_timeout_seconds` (default 45 s). Reproducing a
genuine stall live requires a persona that ingests but does not reply; the
deterministic version is asserted by `fanout_floor_test.go`'s timeout case with
a test-shortened timeout. Live, the symptom of a healthy timeout is that a
silent responder delays the *next* speaker by ~45 s but never blocks the round.

**Verification** (optional):
- [ ] If a responder is silent, the following speaker still posts after ~45 s.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | `group:planning` resolves floor control ON with three persona members | ☐ |
| 2 | Replies land sequentially; mentioned persona (`ember-owl`) speaks first | ☐ |
| 3 | Replies are mutually aware; ≥1 later speaker yields on a covered point | ☐ |
| 4 | REST history confirms order + non-overlapping timestamps | ☐ |
| 5 | (Optional) a silent responder times out at ~45 s without stalling | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: A DM / single-responder channel

Floor control is a no-op below two candidate responders. A DM (always
single-responder) or a group prompt that mentions only one `when_mentioned`
member with no `always` members runs the unchanged concurrent path — there is no
round to serialize. This is the regression-safety guarantee, asserted by
`fanout_floor_test.go`'s single-responder case.

### Edge Case 2: Opting a channel back out

Setting `floor_control: false` on `planning` and restarting must restore the
pre-amendment concurrent shout for that channel (the operator override path,
asserted at the config layer by `TestLoadConfig_FloorControlExplicitFalse`).
Useful as a side-by-side to *see* the baseline this MT contrasts against.

### Edge Case 3: A channel created at runtime (console / REST)

A group channel created **after** startup via the console "New channel" form or
`POST /api/v1/channels` must also reply sequentially — not just channels declared
in `config/channels.yaml`. Create a fresh group channel with two `always`
personas from the console, post one prompt, and confirm the same ordered,
mutually-aware round as Step 3. A persisted runtime channel is re-resolved on
the next restart (it would otherwise revert to the concurrent shout). Asserted
by `TestChannels_CreateChannel_EnablesFloorControl` (create path) and
`TestResolveFloorControl_ConfigAndStoreResident` (restart path).

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| _pending_ | | | | First live run to be recorded against `feature/v036-floor-control-enable`. |

---

## Notes

- "Mutual awareness" is judged from reply **content**; it is inherently a
  qualitative, LLM-output-dependent assertion. The deterministic mechanical
  guarantee (one in-flight dispatch at a time, later speakers' reconstructed
  transcript contains earlier replies) is pinned by `fanout_floor_test.go`; this
  MT validates that the mechanism produces the intended *conversational* effect
  end-to-end with live personas.
- Floor state is **in-process / single-replica** (channels guide §7). This MT
  assumes the default single-orchestrator compose stack.
- The per-turn timeout (45 s) is distinct from the 5 s per-recipient fanout
  timeout — a floor turn waits for a full LLM-composed reply (amendment D2).
