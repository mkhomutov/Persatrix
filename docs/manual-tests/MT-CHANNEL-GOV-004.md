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
  used to reliably draw unanimous Tier B passes
  ([ISSUE-0097](../issues/ISSUE-0097-persona-vote-and-bid-calibration.md)
  defect 1 — **fixed and live-verified** at main @ d51f3b4: an un-mentioned
  opener now draws open-floor replies; see Test Results) — the stall fired on
  round one, before any discussion exists, burning the interaction's CE5
  ration on an empty synthesis. Mentioning every member is still the reliable
  posture for the *arc under test* (it removes opener variance and the
  still-open concurrence pass-proneness from the run); including the
  `addressed` member in the opener also completes the
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

A *sibling* failure where the chair **does** vote but the structure is
lost was the parser bug
[ISSUE-0101](../issues/ISSUE-0101-action-parser-drops-one-line-json-fence.md)
(resolved): a correct `end_interaction_vote` emitted as a **one-line**
` ```json [..] ``` ` markdown fence (spaces, not newlines) was not
extracted by the then-newline-only anchor, so it published as raw JSON
channel text with no vote metadata — and the orchestrator read the missing
vote as an ISSUE-0099 hand-off misfire, producing a visible double
synthesis (the raw-JSON turn, then the re-forced clean one). First seen on
the 2026-06-14 Run A below. The parser is now tolerant of inline / CRLF /
whitespace-padded fences.

---

## Test Results

| Date | Tester | Build | Result | Notes |
|------|--------|-------|--------|-------|
| 2026-06-14 | Claude (operator: mkhomutov) | `feat/issue-0099-resynthesize-trigger` @ c348d18 (HEAD, post-ISSUE-0101 parser fix) | PASS (full arc, steps 1–3) — **natural happy path green; ISSUE-0099 re-force correctly DORMANT** | Re-verify on current HEAD after the ISSUE-0101 parser fix (`c348d18`) landed on top of the b817344 vote-guard. Scope agreed with operator: **natural arc only** (the misfire is no longer naturally LLM-reachable — ISSUE-0101 closed the unparsed-`json`-vote route Run A used, and post-0098 calibration already closed the prose-`@` route; the CLI has no `--vote` to inject, so the *firing* path stays pinned by the deterministic `TestChairResynthesize_*` guards, all green here first). Test-profile pacing (`interaction_idle_timeout_seconds: 180`, reverted after; clean `git diff`), default roster, machine-paced. Interaction `fbc13762`: opener (`--mention` all three) → one risk each (ember-owl perf-cliff, iron-fox ingestion-ceiling, nova-sparrow demo-data) → un-mentioned nudge "Anything else on this?" drew a **silent round** → `channels: stalled round escalated to chair` (`escalation_chair_id=nova-sparrow`, 00:36:02Z). **Step 2:** the chair's forced turn (`6cf18ec7`, 00:36:07Z) persisted **`end_interaction_vote=true` with `mentions=[]`** — a clean synthesis-in-vote, *not* a hand-off → no `mentions name no floor-capable member`, **no misfire, no re-force** (`chair_escalation{outcome=dispatched}=1`, **no `resynthesized`/`already_escalated`/`dispatch_error` label** in Prometheus — the ISSUE-0099 trigger armed and correctly disarmed on the clean synthesis). A `--mention iron-fox` concurrence (`ea8b8eea`, 00:38:32Z) cast a **single-message** vote (`end_interaction_vote=true`, prose folded in — ISSUE-0097 defect-2 fix holding) → `interaction closed by end-of-interaction votes` `trigger=end_votes votes=2 participant_id=iron-fox` (00:38:32Z). **Step 3:** structural ("Conversation ended") summary records the decision (load-testing infra risk + demo-data GTM risk, owners today, fixable before Friday). Same interaction-id segmentation quirk as prior rows (summary `bb81a0ad` vs vote-closed `fbc13762`); concurrence still `--mention`-drawn (open-floor pass-proneness, ISSUE-0097, orthogonal). Net: on current HEAD the ISSUE-0099 fix is **dormant-correct** — the re-force stays silent on a clean synthesis, and the only remaining route to *firing* it live (unparsed/malformed forced turn) is now closed by ISSUE-0101, leaving the firing path covered by the deterministic regression tests. |
| 2026-06-14 | Claude (operator: mkhomutov) | `feat/issue-0099-resynthesize-trigger` @ b817344 (PR 2 + review fix) | PASS — **ISSUE-0099 resynthesize re-force fired LIVE on the REAL chair, no injection** | Re-verify on current HEAD after the PR-2-review fix (`b817344`: a synthesis-in-vote disarms the trigger instead of being misread as a misfire). Deterministic half green first — full `internal/channels` suite incl. the new `TestChairResynthesize_EndToEnd_VoteReplyDisarmsNoReForce` guard. Default roster, machine-paced. **Run A (interaction `5485a09a`):** opener (`--mention` all three) → one concern each → un-mentioned nudge "Anything else on this?" drew a silent round → `stalled round escalated to chair` (`nova-sparrow`, 16:33:44Z). The chair's forced-turn reply was an `end_interaction_vote` action the agent **failed to parse** — it published as a raw ` ```json ` block (msg `5c2e0a61`) carrying **`mentions=1` to a non-floor-capable target and NO vote metadata** → orchestrator correctly classified it a provable misfire: `mentions name no floor-capable member` (16:33:49.501Z) → `chair hand-off misfired; re-forced a synthesize-only turn` (16:33:49.508Z, ~4.7s after escalation) → `chair_escalation{outcome=resynthesized}=1` (no `resynthesize_error`). The re-forced turn cast a **clean prose synthesis** (16:33:52Z); a `--mention iron-fox` concurrence closed it `trigger=end_votes votes=2 participant_id=iron-fox` (16:34:11Z). **Step 3:** structural ("Conversation ended") summary with the decision (EOD-Monday delay/scope) on record (same interaction-id segmentation quirk as prior rows). **This corrects the 0f76b1a row's "misfire is not LLM-reachable" claim — the real chair reached it here without synthetic injection**, via an *unparsed* vote-JSON (not the prose-`@`-handoff route, which the post-0098 calibration still closes). **Run B (interaction @18:10:13Z):** a second natural arc on a fresh topic — escalation fired (`dispatched` 1→2), but the chair's forced turn was a **clean prose synthesis with no `@`-mentions** → `namedNoFloorCapable=false`, no misfire, no re-force (`resynthesized` held at 1). Confirms the trigger correctly stays silent on a clean synthesis. (Arc aborted before the concurrence draw on a windowed-vs-cumulative count bug in my pacing script — orthogonal to the fix; no close.) **Not reproduced live:** the *exact* b817344 vote-guard case (a correctly-**parsed** `end_interaction_vote` that `@`-mentions a non-floor-capable voice → empty floor subset BUT vote present → disarm without re-force). The CLI has no `--vote`/metadata flag, so it cannot be injected; the real chair was nondeterministic across the two arcs (unparsed-JSON-vote in A, prose-no-`@` in B). It stays pinned by the deterministic regression test. **Secondary finding ([ISSUE-0101](../issues/ISSUE-0101-action-parser-drops-one-line-json-fence.md), resolved):** the chair sometimes wraps its `end_interaction_vote` action in a **one-line** ` ```json [..] ``` ` markdown fence the agent action-parser did not strip (the anchor required newlines), so a real synthesis-in-vote published as raw JSON text and read as a misfire (Run A) — a sibling of Edge Case 2. Fixed by making the fence extraction tolerant of inline / CRLF / whitespace-padded fences, with one-line-fence regression tests in `test_action_parser_prose.py`. Net: the ISSUE-0099 re-force happy path is now **live-proven on the real chair** (recovers a misfired/malformed forced turn into a recorded close), and correctly silent on clean synthesis; the vote-disarm refinement is deterministically green. |
| 2026-06-13 | Claude (operator: mkhomutov) | `feat/issue-0099-resynthesize-trigger` @ 0f76b1a (PR 2) | PASS — **ISSUE-0099 resynthesize trigger verified live** (happy path via synthetic chair-misfire injection) | First live exercise of the ISSUE-0099 fix (re-force a synthesize-only turn on a provable hand-off misfire). Deterministic half green first. Lever: temporary `ember-owl: observer` override (non-floor-capable hand-off target; reverted) + `interaction_idle_timeout_seconds: 180`. **Key finding — the misfire is not LLM-reachable under the shipped chair prompt:** four arcs of escalating provocation (un-asked observer; security-only decision; ember-owl framed as sole sign-off authority that only acts on `@`-mentions, both peers `@`-pinging it) — **all four nova synthesized in prose and named the missing voice WITHOUT `@`** → no lift → `misfired=false`; the trigger ran each time and correctly **disarmed** (no metric, no false positive). The post-ISSUE-0098 synthesis calibration structurally closes the bare-`@`-hand-off route, reconfirming the `low`/narrow-residue scope. **Happy-path proof (synthetic injection):** injected the misfiring chair reply via the body-trusted CLI (`channel send --as nova-sparrow "@Ember Owl …"`, no vote) into a fresh escalated interaction, racing the armed stash and winning (~74 ms): `escalated to chair` → `lifted ["ember-owl"]` → `mentions name no floor-capable member` → `chair hand-off misfired; re-forced a synthesize-only turn`; `chair_escalation{outcome=resynthesized}=1` (no `resynthesize_error`). The re-forced turn cast a real synthesis-in-vote; a `--mention iron-fox` concurrence closed it `trigger=end_votes votes=2`. **Step 3:** structural ("Conversation ended") summary with the decision recorded. Caveat: the injection raced nova's real reply, so nova emitted two near-identical synthesis turns — a test-method artifact, not the fix. Net: correct in-test, dormant-correct live (wired in, no false positives), happy path fires as designed when the residue is present. |
| 2026-06-13 | Claude (operator: mkhomutov) | main @ cf02a53 (PR 3) | PASS (steps 1–3) — **ISSUE-0097 defect 2 RESOLVED** | Live re-verify of the structural prose-fold (PR 3, #627), **single-concurrer** scenario. Default roster, machine-paced, interaction `bfe2386e`: opener (`--mention` all three) → one risk each → un-mentioned nudge drew a silent round → escalation (`nova-sparrow`, 12:09:23Z). Chair's forced turn was a clean single-message synthesis-in-vote (`cc833960`, `end_interaction_vote=true`). Drew **one** concurrer (`--mention iron-fox` only) — the exact case the d51f3b4 row predicted would miss. It no longer misses: iron-fox emitted exactly **one** publish (`vote=true`, prose *"Agreed…"* folded **into** `content`), not the d51f3b4 two-message split — `fold_prose_into_end_vote` kept it to one turn, in-window, and the quorum closed (`trigger=end_votes votes=2 participant_id=iron-fox`, 12:09:33Z), a genuine single-concurrer close (not d51f3b4's incidental two-within-W). Step 3: structural ("Conversation ended") with the synthesis on record. Defect 1 already closed → **ISSUE-0097 fully resolved**. Orthogonal pre-existing quirk: concurrence still had to be `--mention`-drawn (open-floor pass-proneness, secondary item). |
| 2026-06-13 | Claude (operator: mkhomutov) | main @ d51f3b4 | PARTIAL — defect 1 PASS, defect 2 FAIL | **[ISSUE-0097](../issues/ISSUE-0097-persona-vote-and-bid-calibration.md) live verification** with PR 1 (#624) + PR 2 (#625) both landed. **Defect 1 (opening-round bid pass) — RESOLVED:** an *un-mentioned* opener ("Name exactly one risk each…") drew open-floor replies with **no `--mention`** — iron-fox + nova-sparrow each posted a distinct risk (ember-owl `addressed`, silent by design); pre-fix this exact round drew unanimous Tier B passes and stalled on round one. PR 1's bid calibration confirmed working live. **Defect 2 (split prose+vote concurrence) — NOT resolved:** on an all-`participant` override (reverted after) the stall escalated (`nova-sparrow`, interaction `ac4063c4`, 10:38:41Z) and Nova's forced turn was a correct **single-message** synthesis-in-vote (`end_interaction_vote=true`, synthesis in `content` — chair path clean). But both drawn concurrers still split prose + vote into two messages 4–5ms apart — ember-owl `9dae857c` (prose) → `b58d4ecd` (vote) @ .786/.791; iron-fox `e2a04ad8` (prose) → `4084bb9f` (vote) @ .745/.749 — each one `claude-sonnet-4-6` turn emitting a text block + action block, persisted as two channel messages. PR-2 steer confirmed baked (`end-interaction-vote.md` line 9, "one message, not two") but ineffective: the split is **structural**, not a prompt miss. The close landed (`trigger=end_votes votes=2`, `participant_id=iron-fox`) only **incidentally** — the two concurrers' split votes fell within W=3 of *each other* (Nova's vote already out of window); a *single* concurrer would reproduce the original out-of-window miss. **Step 3 PASS:** the closed interaction's summary renders "ended" (structural) with the decision (Wednesday-EOD load-test gate). Same interaction-id segmentation quirk as prior rows (summary under `952e3e69`). Also reconfirmed: the chair's synthesis drew **no spontaneous** open-floor concurrence (both bid-passed; votes had to be `--mention`-drawn) — PR 1 calibrated for unanswered *opener* questions, not concurrence on a chair synthesis. Finding → ISSUE-0097 defect 2 **re-opened**: needs a runtime/serialization fix (suppress/fold a turn's free text when it carries an `end_interaction_vote` action), prompt steering insufficient. |
| 2026-06-13 | Claude (operator: mkhomutov) | main @ 7b2de85 | PASS (full arc, steps 1–3) | Clean re-run to re-confirm **[ISSUE-0099](../issues/ISSUE-0099-ce5-ration-spent-on-provably-failed-handoff.md) severity** on the **default roster** (`ember-owl: addressed` — the standing voice whose presence drove the 2026-06-12 three-for-three hand-offs), deliberately *not* the PR-622 all-`participant` override. Ran twice; **both arcs synthesized, neither handed off** — the ISSUE-0099 failed-hand-off path was not entered. Canonical run on the single interaction `21fe95de` (default 600s window): opener (`--mention` all three) → one risk each → un-mentioned nudge "Anything else on this?" drew a silent round → `channels: stalled round escalated to chair` (`escalation_chair_id=nova-sparrow`, 06:38:32Z) → the forced turn was a synthesis-in-vote ("Good place to close. Three distinct risks on the record …", persisted `end_interaction_vote=true` with **`mentions=[]`** — a synthesis, *not* a hand-off) → ember-owl + iron-fox each concurred (prose "Agreed" + `end_interaction_vote=true`) → `channels: interaction closed by end-of-interaction votes` `trigger=end_votes votes=2` (06:39:07Z). Metric `channel.conversation.chair_escalation{outcome=dispatched}=1` — no `already_escalated`/`dispatch_error`. **Step 3:** the closed interaction's summary records the go/no-go-gates closure; structural ("ended") close. **Finding → ISSUE-0099 re-scoped to `low`**: post-0096/0098 the common hand-off failure is gone, leaving only the narrow `respond:never`-observer / chair-self empty-target residue. Two pre-existing quirks reconfirmed, both orthogonal to ISSUE-0099: (a) concurrence had to be `--mention`-drawn (open-floor pass-proneness, ISSUE-0097); (b) the agent `/interactions` summary segmented under a different id (`a08b7305`) than the message-stamped/vote-closed `21fe95de` (the same segmentation note as the PR-622 row). A first pass with a 180s test-profile idle window reached the same outcome but split the arc across interactions on idle rotation (synthesis on `96254145` idle-closed; concurrence votes closed the follow-on `052d16f0` on `end_votes`); the window override was reverted for the canonical run above. |
| 2026-06-13 | Claude (operator: mkhomutov) | main @ 3cde982 (PR 622) | PASS (full arc, steps 1–3) | First live exercise of **outcome (a)** — the synthesis-in-vote ISSUE-0098 had made unreachable (prior runs went 3-for-3 on hand-off). Run on the **all-`participant`** planning roster (temporary `ember-owl: participant` override, reverted after — the lever the PR 622 review identified: no `addressed`-only standing voice). Arc: opener (`--mention` all three) drew one risk each → un-mentioned nudge "Anything else on this?" drew silence → `channels: stalled round escalated to chair` (`escalation_chair_id=nova-sparrow`, interaction `4b332af1`, 05:34:27Z). **Step 2:** nova-sparrow's forced turn was a genuine synthesis ("Three distinct risks on the record … Recommend these three go into the v0.3.0 launch checklist as explicit go/no-go gates") carried **inside the vote** — persisted `metadata.end_interaction_vote=true`, not prose beside the block. **Close:** iron-fox + ember-owl each concurred (prose "Agreed" + `end_interaction_vote=true`); `channels: interaction closed by end-of-interaction votes` `trigger=end_votes votes=2` (05:51:05Z). **Step 3:** summary records the resolution ("… confirmation … on Nova's summary of three risks … agreed to proceed with closure"), `close_reason=structural` ("ended"). Two caveats, both pre-existing and orthogonal to ISSUE-0098: (a) concurrence had to be *drawn* — personas bid-passed on open-floor nudges, so a `--mention`-targeted nudge was needed to pull the two votes (ISSUE-0097 pass-proneness); (b) the agent `/interactions/closed` summary listed ids (`0d2ca73d`, `3eb8c3e5`) diverging from the message-stamped/end-vote-closed id (`4b332af1`) — interaction-id segmentation in the summary view, worth a separate look. |
| 2026-06-13 | Claude (operator: mkhomutov) | main @ def19ca | PASS (ISSUE-0096 mechanism) | Targeted live verification of the display-name-mention-lifting fix (#617–#619), **not** the full stall→escalation arc. Lever: `ember-owl` is `respond: addressed` (when_mentioned), so it wakes *only* on a real mention. Joined as `alex --respond never` and sent `@Ember Owl — gut check…` with **no `--mention`** (prose only — the exact form the prior FAIL row proved reached nobody). Three-way proof the lift now works end-to-end through the real stack: (1) orchestrator DEBUG `channels: lifted display-name mentions from content` `lifted=["ember-owl"]` (`channel_mention_lift.go:124`); (2) the persisted row (`deeb6367`) carries `mentions=["ember-owl"]` despite the empty structured array — the canonical id was unioned in before persist/fanout; (3) `ember-owl` **replied** (impossible pre-fix for a when_mentioned member on prose). Bonus: a follow-up `nova-sparrow` turn's prose `@alex` also lifted (`mentions=["ember-owl","alex"]`), and `ember-owl`'s reply-to-human (`mentions=["alex"]`, `respond:never`) correctly logged `mentions name no floor-capable member` — the floor-capable basis is unchanged, it just finally sees the addressees. This closes the ISSUE-0096 resolver bug. The native **Edge Case 1** observation (a *chair forced-turn* hand-off by display name restarting a stalled discussion) is the same mechanism inside the governance arc and is still gated on ISSUE-0098's chair completeness-fixation; left for an opportunistic full-arc run. |
| 2026-06-12 | Claude (operator: mkhomutov) | main @ d47385d | FAIL (blocked) | Re-run targeting step 3 after the end-vote close-propagation fix (#613–#615). Two interactions, neither reached a vote-close: both stalls escalated correctly (`outcome=dispatched`), but both chair forced turns chose hand-off (outcome b) on the silent `addressed` member, named it by display name (ISSUE-0096 ×2), reached nobody, and the interactions deadlocked to idle — CE5 ration spent, concurrence nudges drew honest passes with no synthesis on the table (ISSUE-0098/ISSUE-0099 filed from this run). Steps 2–3 not exercised; close-propagation fix still unverified live. Also confirmed: bare-compose preconditions crash-loop agents (provider overlay required); `already_escalated` is metric-only (log-greps never fire); CLI in-text @-names are prose (structured `--mention` required); interaction rotation does not reset persona windows — re-asked questions get deflected as duplicates, so re-runs need a fresh topic. |
| 2026-06-12 | Claude (operator: mkhomutov) | main @ 113c728 | PARTIAL PASS | Steps 1–2 fully verified (run with interaction `ebc02462`: stall → `outcome=dispatched` → chair synthesis-in-vote with `end_interaction_vote: true` on the wire → close on 2nd distinct vote, `trigger=end_votes`, 9 s after escalation; CE5 one-ration guard observed three times). Step 3 partial: summaries carry the synthesis and vote-closed interactions render "ended", but the closing vote's fanout suppression means no member's agent-local tracker hears the close — with no follow-up traffic inside the agent-side 600 s idle window every member's surface renders the escalated interaction "went idle". Edge Case 1 (chair hand-off) observed on first run, incl. display-name @-mentions resolving to no floor-capable member. Side findings: one unreproduced idle-rotation no-fire (700 s gap, window 600 s, 03:14:50→03:26:30Z; later gaps of 680 s did rotate); personas pass-prone enough that un-mentioned prompts often stall on the *opening* round; split prose+vote replies burn a W=3 turn. Wall-clock cost was ~2 h — dominated by 600 s governance timers and re-runs; this MT needs a test-profile idle window (e.g. 60 s) to be practical. |

## Notes

- The escalation ration is per-interaction (CE5): a fresh topic gets a fresh
  ration, so repeated runs in one channel work without resets as long as
  each closes or idles out.
