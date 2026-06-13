# Manual Test MT-CHANNEL-GOV-004: Chair stall escalation — a stalled discussion ends in a recorded decision

**Test ID**: `MT-CHANNEL-GOV-004`
**Feature Area**: Channels (conversation governance — the RFC 0030 chair-stall-escalation amendment, a minimal Layer 5 slice)
**Version**: 1.1
**Created**: 2026-06-11
**Last Updated**: 2026-06-12
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
ENABLE_UI=1 docker compose -f docker-compose.yaml -f docker-compose.anthropic.yaml up --build
```

The provider overlay is required: the base config ships UNCONFIGURED by
design (RFC 0033 — no default provider), so a bare
`docker compose up --build` crash-loops every agent on the missing
`quality` model alias. Any provider lane works (`make demo-anthropic`
is the one-step equivalent of the above).

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
  --as alex --mention iron-fox --mention nova-sparrow --mention ember-owl
# …after the round lands:
./bin/persatrix channel send planning \
  "Anything else on this?" \
  --as alex
```

Two send-side facts the 2026-06-12 session tripped on:

- **Mentions only travel via `--mention`.** The CLI does not parse
  in-text `@id` (or `@Display Name`) from the message body — the
  mention list is a structured field on the publish request. An opener
  whose only addressing is prose lands on the open floor.
- **Mention every member in the opener.** Open-floor opening questions
  reliably draw unanimous Tier B passes
  ([ISSUE-0097](../issues/ISSUE-0097-persona-vote-and-bid-calibration.md)) —
  the stall fires on round one, before any discussion exists, burning
  the interaction's CE5 ration on an empty synthesis. Mentioning every
  member (including the `addressed` one) also completes the
  enumeration, which matters for the chair's disposition: a visibly
  missing voice steers the forced turn to hand-off (outcome b) instead
  of synthesis
  ([ISSUE-0098](../issues/ISSUE-0098-chair-completeness-fixation-blocks-synthesis.md)).
  The *nudge* stays un-mentioned — that is the honest stall under test.
- **To exercise outcome (a), run with an all-`participant` roster.** On
  the default roster `ember-owl` is `respond: addressed` — a
  genuinely-unasked standing voice, which the calibrated outcome (b) still
  legitimately lets the chair hand off to (ISSUE-0098 Resolution). The
  2026-06-13 PR 622 PASS overrode `ember-owl: participant` in
  `config/channels.yaml` (reverted after the run) so no `addressed`-only
  voice stood open when the stall hit; that is what steered the chair to
  synthesize rather than hand off.

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
- If the synthesis round *also* stalls, the orchestrator increments
  `chair_escalation{outcome=already_escalated}` and nothing else — CE5's
  one-ration guard; nudge once more from `alex` to draw the concurrence.
  Note this disposition is **metric-only**: the CE5 branch emits no log
  line, so check Prometheus (`channel.conversation.chair_escalation`),
  not the orchestrator logs — a log-grep for it never fires.

**Verification**:
- [x] The chair's turn carries a synthesis, not a hollow sign-off. *(2026-06-13, PR 622 — `end_interaction_vote=true` with a real three-risk synthesis in `content`.)*
- [x] The close lands on the second distinct vote. *(2026-06-13 — `trigger=end_votes votes=2`.)*

### Step 3: The synthesis is the recorded outcome

```bash
./bin/persatrix agent interactions nova-sparrow --limit 1
```

**Expected**:
- The closed interaction's summary names the synthesis (the risks/decision) —
  the stall ended in a recorded decision, not an idle trail-off. The close
  trigger renders as **"ended"** (structural), not *went idle*.

**Verification**:
- [x] The summary carries the chair's synthesis; the trigger is "ended". *(2026-06-13, PR 622 — summary records the resolution around Nova's synthesis; `close_reason=structural`.)*

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | An honest stall (silent round) escalates: one forced turn to the chair, `outcome=dispatched` | ✅ (2026-06-13, PR 622) |
| 2 | The chair publishes synthesis-in-vote; a second vote closes (`trigger=end_votes`); CE5 caps re-escalation | ✅ (2026-06-13, PR 622) |
| 3 | The interaction summary records the synthesis with a structural ("ended") close | ✅ (2026-06-13, PR 622) |

---

## Edge Cases & Error Scenarios

### Edge Case 1: The chair hands off instead (outcome b)

A legitimate alternative: the chair @-mentions the member best placed with
the specific open point instead of synthesizing. The named member's reply
restarts the discussion — no close yet, and that is correct behaviour, not a
failure. To exercise outcome (a) instead, remove the `addressed`-only
standing voice (all-`participant` roster, per the step-1 override note).

Pre-PR-622 calibration ran three-for-three on outcome (b), always
triggered by a member who never spoke
([ISSUE-0098](../issues/ISSUE-0098-chair-completeness-fixation-blocks-synthesis.md),
now **resolved** — the calibrated snippet plus an all-`participant` roster
exercised outcome (a) live on 2026-06-13, see Test Results) —
and the hand-off itself names members by display name, which used to resolve
to nobody ([ISSUE-0096](../issues/ISSUE-0096-display-name-mentions-resolve-to-nobody.md)
— **fixed** 2026-06-13 by the
[display-name-mention-lifting amendment](../rfcs/0011-amendment-display-name-mention-lifting.md),
verified live: the publish seam now lifts `@Display Name` to the member id, so
a display-name hand-off reaches the named member). Before that fix, instead of
restarting the discussion it deadlocked the interaction: CE5
blocks re-escalation
([ISSUE-0099](../issues/ISSUE-0099-ce5-ration-spent-on-provably-failed-handoff.md))
and only idle rotation ends it. Until those land, the practical guard is
the step-1 posture: mention every member in the opener so no voice is
visibly missing when the stall hits.

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
| 2026-06-13 | Claude (operator: mkhomutov) | main @ 3cde982 (PR 622) | PASS (full arc, steps 1–3) | First live exercise of **outcome (a)** — the synthesis-in-vote ISSUE-0098 had made unreachable (prior runs went 3-for-3 on hand-off). Run on the **all-`participant`** planning roster (temporary `ember-owl: participant` override, reverted after — the lever the PR 622 review identified: no `addressed`-only standing voice). Arc: opener (`--mention` all three) drew one risk each → un-mentioned nudge "Anything else on this?" drew silence → `channels: stalled round escalated to chair` (`escalation_chair_id=nova-sparrow`, interaction `4b332af1`, 05:34:27Z). **Step 2:** nova-sparrow's forced turn was a genuine synthesis ("Three distinct risks on the record … Recommend these three go into the v0.3.0 launch checklist as explicit go/no-go gates") carried **inside the vote** — persisted `metadata.end_interaction_vote=true`, not prose beside the block. **Close:** iron-fox + ember-owl each concurred (prose "Agreed" + `end_interaction_vote=true`); `channels: interaction closed by end-of-interaction votes` `trigger=end_votes votes=2` (05:51:05Z). **Step 3:** summary records the resolution ("… confirmation … on Nova's summary of three risks … agreed to proceed with closure"), `close_reason=structural` ("ended"). Two caveats, both pre-existing and orthogonal to ISSUE-0098: (a) concurrence had to be *drawn* — personas bid-passed on open-floor nudges, so a `--mention`-targeted nudge was needed to pull the two votes (ISSUE-0097 pass-proneness); (b) the agent `/interactions/closed` summary listed ids (`0d2ca73d`, `3eb8c3e5`) diverging from the message-stamped/end-vote-closed id (`4b332af1`) — interaction-id segmentation in the summary view, worth a separate look. |
| 2026-06-13 | Claude (operator: mkhomutov) | main @ def19ca | PASS (ISSUE-0096 mechanism) | Targeted live verification of the display-name-mention-lifting fix (#617–#619), **not** the full stall→escalation arc. Lever: `ember-owl` is `respond: addressed` (when_mentioned), so it wakes *only* on a real mention. Joined as `alex --respond never` and sent `@Ember Owl — gut check…` with **no `--mention`** (prose only — the exact form the prior FAIL row proved reached nobody). Three-way proof the lift now works end-to-end through the real stack: (1) orchestrator DEBUG `channels: lifted display-name mentions from content` `lifted=["ember-owl"]` (`channel_mention_lift.go:124`); (2) the persisted row (`deeb6367`) carries `mentions=["ember-owl"]` despite the empty structured array — the canonical id was unioned in before persist/fanout; (3) `ember-owl` **replied** (impossible pre-fix for a when_mentioned member on prose). Bonus: a follow-up `nova-sparrow` turn's prose `@alex` also lifted (`mentions=["ember-owl","alex"]`), and `ember-owl`'s reply-to-human (`mentions=["alex"]`, `respond:never`) correctly logged `mentions name no floor-capable member` — the floor-capable basis is unchanged, it just finally sees the addressees. This closes the ISSUE-0096 resolver bug. The native **Edge Case 1** observation (a *chair forced-turn* hand-off by display name restarting a stalled discussion) is the same mechanism inside the governance arc and is still gated on ISSUE-0098's chair completeness-fixation; left for an opportunistic full-arc run. |
| 2026-06-12 | Claude (operator: mkhomutov) | main @ d47385d | FAIL (blocked) | Re-run targeting step 3 after the end-vote close-propagation fix (#613–#615). Two interactions, neither reached a vote-close: both stalls escalated correctly (`outcome=dispatched`), but both chair forced turns chose hand-off (outcome b) on the silent `addressed` member, named it by display name (ISSUE-0096 ×2), reached nobody, and the interactions deadlocked to idle — CE5 ration spent, concurrence nudges drew honest passes with no synthesis on the table (ISSUE-0098/ISSUE-0099 filed from this run). Steps 2–3 not exercised; close-propagation fix still unverified live. Also confirmed: bare-compose preconditions crash-loop agents (provider overlay required); `already_escalated` is metric-only (log-greps never fire); CLI in-text @-names are prose (structured `--mention` required); interaction rotation does not reset persona windows — re-asked questions get deflected as duplicates, so re-runs need a fresh topic. |
| 2026-06-12 | Claude (operator: mkhomutov) | main @ 113c728 | PARTIAL PASS | Steps 1–2 fully verified (run with interaction `ebc02462`: stall → `outcome=dispatched` → chair synthesis-in-vote with `end_interaction_vote: true` on the wire → close on 2nd distinct vote, `trigger=end_votes`, 9 s after escalation; CE5 one-ration guard observed three times). Step 3 partial: summaries carry the synthesis and vote-closed interactions render "ended", but the closing vote's fanout suppression means no member's agent-local tracker hears the close — with no follow-up traffic inside the agent-side 600 s idle window every member's surface renders the escalated interaction "went idle". Edge Case 1 (chair hand-off) observed on first run, incl. display-name @-mentions resolving to no floor-capable member. Side findings: one unreproduced idle-rotation no-fire (700 s gap, window 600 s, 03:14:50→03:26:30Z; later gaps of 680 s did rotate); personas pass-prone enough that un-mentioned prompts often stall on the *opening* round; split prose+vote replies burn a W=3 turn. Wall-clock cost was ~2 h — dominated by 600 s governance timers and re-runs; this MT needs a test-profile idle window (e.g. 60 s) to be practical. |

## Notes

- The escalation ration is per-interaction (CE5): a fresh topic gets a fresh
  ration, so repeated runs in one channel work without resets as long as
  each closes or idles out.
