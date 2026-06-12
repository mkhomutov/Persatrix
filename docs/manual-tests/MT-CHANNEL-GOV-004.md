# Manual Test MT-CHANNEL-GOV-004: Chair stall escalation — a stalled discussion ends in a recorded decision

**Test ID**: `MT-CHANNEL-GOV-004`
**Feature Area**: Channels (conversation governance — the RFC 0030 chair-stall-escalation amendment, a minimal Layer 5 slice)
**Version**: 1.0
**Created**: 2026-06-11
**Last Updated**: 2026-06-11
**Status**: Active

---

## Overview

**Purpose**: Verify the chair-stall-escalation arc end-to-end with real LLMs —
the live half of the
[amendment](../rfcs/0030-amendment-chair-stall-escalation.md)'s acceptance
(§C item 3). When a floor round ends with **zero replies** on an open
interaction — every participant honestly bid "nothing new to add" with the
question unresolved — the orchestrator now dispatches **one forced turn** to
the channel's `escalation_chair_id`. The chair's prompt
([`chair-escalation.md`](../../prompts/runtime/safety/chair-escalation.md))
forbids silence for that turn and steers the synthesis into the
`end_interaction_vote` action's `content`, so the synthesis and the vote
travel as one publish; a second member's concurrence closes the discussion
with the synthesis on the record. The visible contract: *a discussion that
stalls no longer dies into idle rotation with its outcome unrecorded — it
ends in a chair synthesis the summary surface hands back.*

The deterministic half is pinned by
[`TestConvergence_StallEscalatesAndClosesByVotes`](../../internal/channels/interaction_convergence_test.go);
this MT covers what automation cannot: whether a real persona, prompted only
by the escalation framing, produces an honest synthesis-in-vote (§C item 3
requires the **honest bid-pass** stall — every persona alive and bidding —
because CE1's detector reads outcomes, not reasons; only the semantic stall
exercises the synthesis half rather than detection alone).

**Scope**: the default `planning` group channel — `escalation_chair_id:
nova-sparrow` ships in the demo config — one prompt engineered to stall
after a short discussion, and the stall → escalation → synthesis+vote →
concurrence → close → summary arc.

**Out of scope**: detection edge cases (the automated matrix in
[`chair_escalation_test.go`](../../internal/channels/chair_escalation_test.go)
pins them), vote-convergence without a stall
([MT-CHANNEL-GOV-003](MT-CHANNEL-GOV-003.md)).

---

## Related Documentation

- [Chair-stall-escalation amendment](../rfcs/0030-amendment-chair-stall-escalation.md) — CE1–CE7; this MT is its live acceptance
- [channels guide §Conversation governance](../guides/channels.md#conversation-governance-rfc-0030-layers-124--v038) — operator-facing behaviour
- [`chair-escalation.md`](../../prompts/runtime/safety/chair-escalation.md) — the forced-turn framing

**Related Automated Tests**:
- [`interaction_convergence_test.go`](../../internal/channels/interaction_convergence_test.go) — the deterministic escalation arc (stall → forced turn → synthesis-vote → concurrence → close; the synthesis round's stall observes CE5, not a second escalation)
- [`chair_escalation_test.go`](../../internal/channels/chair_escalation_test.go) — the detection/disposition matrix (CE1–CE7, orchestrator half)
- [`test_chair_escalation_agent.py`](../../tests/unit/python/test_chair_escalation_agent.py) — the gate admit, Tier B bypass, framing (agent half)

---

## Preconditions

Same as [MT-CHANNEL-GOV-003 § Preconditions](MT-CHANNEL-GOV-003.md#preconditions)
(valid API key; clean state; the default `config/channels.yaml` — it carries
`escalation_chair_id: nova-sparrow` on `planning`).

```bash
make reset
ENABLE_UI=1 docker compose up --build
```

---

## Test Procedure

### Step 1: Engineer an honest stall

Pose a question the personas can each contribute one point to and then
genuinely exhaust — a bounded enumeration works well. Let the first round
land, then nudge once with a follow-up that adds nothing new (so every bid
honestly passes).

```bash
./bin/persatrix channel join planning --as alex --respond never
./bin/persatrix channel send planning \
  "Name exactly one risk each for shipping v1 next Friday. One sentence per person, no repeats." \
  --as alex
# …after the round lands:
./bin/persatrix channel send planning \
  "Anything else on this?" \
  --as alex
```

**Expected**:
- The first prompt draws the round; the follow-up draws **silence** (every
  Tier B bid passes — the points are made) and the floor round times out
  turn by turn.
- At the stalled round's end the orchestrator logs
  `channels: stalled round escalated to chair` and emits
  `chair_escalation{outcome=dispatched}`.

**Verification**:
- [ ] The follow-up round produces no replies, then the escalation log line
      appears with `escalation_chair_id=nova-sparrow`.

### Step 2: The chair synthesizes and votes

**Expected**:
- `nova-sparrow` (and only it) takes one more turn: a message whose content
  is a genuine synthesis (the named risks, the recommendation) — **the vote's
  `content`**, not a bare sign-off — visible in the timeline as its message,
  with the vote flag on the wire.
- The synthesis is fresh open-floor stimulus: the other participants' bids
  re-judge it ("do I agree?"); a second persona concurs with its own vote
  within the W=3 window.
- `interaction_closed{trigger=end_votes}` fires; the room then stays quiet.
- If the synthesis round *also* stalls, the orchestrator emits
  `chair_escalation{outcome=already_escalated}` and nothing else — CE5's
  one-ration guard; nudge once more from `alex` to draw the concurrence.

**Verification**:
- [ ] The chair's turn carries a synthesis, not a hollow sign-off.
- [ ] The close lands on the second distinct vote.

### Step 3: The synthesis is the recorded outcome

```bash
./bin/persatrix agent interactions nova-sparrow --limit 1
```

**Expected**:
- The closed interaction's summary names the synthesis (the risks/decision) —
  the stall ended in a recorded decision, not an idle trail-off. The close
  trigger renders as **"ended"** (structural), not *went idle*.

**Verification**:
- [ ] The summary carries the chair's synthesis; the trigger is "ended".

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | An honest stall (silent round) escalates: one forced turn to the chair, `outcome=dispatched` | ☐ |
| 2 | The chair publishes synthesis-in-vote; a second vote closes (`trigger=end_votes`); CE5 caps re-escalation | ☐ |
| 3 | The interaction summary records the synthesis with a structural ("ended") close | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: The chair hands off instead (outcome b)

A legitimate alternative: the chair @-mentions the member best placed with
the specific open point instead of synthesizing. The named member's reply
restarts the discussion — no close yet, and that is correct behaviour, not a
failure. Re-run with a more exhausted topic to exercise outcome (a).

### Edge Case 2: The chair narrates instead of voting

If the chair's turn produces prose with no vote action, the parser's
vote-scoped rescue cannot fire and the turn lands as an ordinary message —
the discussion may still close on later votes, but the arc degrades. Treat
as prompt-calibration feedback on `chair-escalation.md` (PR #610's review
steered the snippet hard toward synthesis-in-vote; persistent disobedience
here is signal that steering needs another pass).

---

## Test Results

| Date | Tester | Build | Result | Notes |
|------|--------|-------|--------|-------|
| 2026-06-12 | Claude (operator: mkhomutov) | main @ 113c728 | PARTIAL PASS | Steps 1–2 fully verified (run with interaction `ebc02462`: stall → `outcome=dispatched` → chair synthesis-in-vote with `end_interaction_vote: true` on the wire → close on 2nd distinct vote, `trigger=end_votes`, 9 s after escalation; CE5 one-ration guard observed three times). Step 3 partial: summaries carry the synthesis and vote-closed interactions render "ended", but the closing vote's fanout suppression means no member's agent-local tracker hears the close — with no follow-up traffic inside the agent-side 600 s idle window every member's surface renders the escalated interaction "went idle". Edge Case 1 (chair hand-off) observed on first run, incl. display-name @-mentions resolving to no floor-capable member. Side findings: one unreproduced idle-rotation no-fire (700 s gap, window 600 s, 03:14:50→03:26:30Z; later gaps of 680 s did rotate); personas pass-prone enough that un-mentioned prompts often stall on the *opening* round; split prose+vote replies burn a W=3 turn. Wall-clock cost was ~2 h — dominated by 600 s governance timers and re-runs; this MT needs a test-profile idle window (e.g. 60 s) to be practical. |

## Notes

- The escalation ration is per-interaction (CE5): a fresh topic gets a fresh
  ration, so repeated runs in one channel work without resets as long as
  each closes or idles out.
