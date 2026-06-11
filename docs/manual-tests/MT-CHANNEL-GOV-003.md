# Manual Test MT-CHANNEL-GOV-003: Convergence — a discussion ends because the participants said so

**Test ID**: `MT-CHANNEL-GOV-003`
**Feature Area**: Channels (conversation governance — RFC 0030 Layers 0/4 + the interaction-id producer)
**Version**: 1.0
**Created**: 2026-06-10
**Last Updated**: 2026-06-10
**Status**: Active

---

## Overview

**Purpose**: Verify the v0.3.8 convergence promise end-to-end with real LLMs —
the live-LLM half of the
[interaction-id producer plan](../rfcs/0030-interaction-id-producer-pr-plan.md)'s
acceptance, executed under the [v0.3.8 master plan](../v0.3.8-plan.md)'s
Phase 3 (release-prep). A multi-persona discussion now has a **semantic
terminator**: every publish is stamped with an orchestrator-minted
`interaction_id`, personas carry the end-of-discussion vote vocabulary in
their system prompt, and when **K=2 distinct** participants emit
`END_INTERACTION_VOTE` within **W=3 consecutive** turns the interaction
**closes** — fanout stops, the interaction-summary surface hands back a
readable outcome, and the channel's next message opens a fresh interaction.
The visible contract: *a discussion that reaches its natural end stops
because the participants said so — **before** the cascade-depth backstop
ends it by length — and the room is immediately usable for the next topic.*

The deterministic half of this arc is pinned by the automated
[`interaction_convergence_test.go`](../../internal/channels/interaction_convergence_test.go);
this MT covers what automation cannot — whether real personas, prompted only
by the [`end-interaction-vote`](../../prompts/runtime/safety/end-interaction-vote.md)
snippet, actually judge "we are done" and emit the structured vote action.

**Scope**: the default `planning` group channel (`iron-fox` and
`nova-sparrow` as `participant`, `ember-owl` as `addressed`), one
deliberately *closable* human prompt (a question with a clearly reachable
answer, so the personas have something to converge on), and the close →
summary → fresh-interaction arc observed through the web console and REST.

**Out of scope**: the Layer 1 cost-ceiling close (needs a configured
budget — calibration is post-soak), Tier B salience suppression quality
([MT-CHANNEL-RELEVANCE-002](MT-CHANNEL-RELEVANCE-002.md)), and floor-control
ordering ([MT-CHANNEL-GOV-002](MT-CHANNEL-GOV-002.md)).

---

## Related Documentation

- [Interaction-id producer PR plan](../rfcs/0030-interaction-id-producer-pr-plan.md) — IP1–IP8; this MT is the live-LLM half of its acceptance
- [RFC 0030 §H](../rfcs/0030-multi-agent-conversation-governance.md#h-layer-4--end-of-interaction-signal) — the end-vote quorum design
- [channels guide §Conversation governance](../guides/channels.md#conversation-governance-rfc-0030-layers-124--v038) — operator-facing behaviour
- [`end-interaction-vote.md`](../../prompts/runtime/safety/end-interaction-vote.md) — the prompt half of the social contract

**Related Automated Tests**:
- [`interaction_convergence_test.go`](../../internal/channels/interaction_convergence_test.go) — the deterministic arc (votes close before the depth cap; racer suppressed; fresh interaction after)
- [`interaction_resolver_test.go`](../../internal/channels/interaction_resolver_test.go) — the producer matrix (mint/reuse/rotate/override/close)
- [`test_end_interaction_vote_action.py`](../../tests/unit/python/test_end_interaction_vote_action.py) — the agent-side vote publish + channel binding

---

## Preconditions

Same as [MT-CHANNEL-GOV-002 § Preconditions](MT-CHANNEL-GOV-002.md#preconditions)
(valid API key — the persona replies and votes are real LLM calls; the demo
stack up; `--enable-ui`), **plus**:

- ☐ Clean state (`make reset` or a fresh `PERSATRIX_EPOCH`).
- ☐ The default `config/channels.yaml` is unedited (`end_vote_threshold: 2`,
  `end_vote_window: 3` on `planning`).

```bash
make reset
ENABLE_UI=1 docker compose up --build
```

---

## Test Procedure

### Step 1: Open a closable question

A question with a reachable answer gives the personas something to converge
on; an open-ended ideation prompt postpones the votes indefinitely.

```bash
./bin/persatrix channel join planning --as alex --respond never
./bin/persatrix channel send planning \
  "Quick decision needed: do we name the new service 'relay' or 'beacon'? Pick one, give one reason, and wrap up when you agree." \
  --as alex
```

**Expected**:
- `iron-fox` and `nova-sparrow` reply in an ordered round (floor control).
- Every persisted message carries the same orchestrator-minted
  `interaction_id` in its metadata. (There is no per-publish stamp log —
  the resolver only logs when it *overrides* a divergent claim, which this
  traffic does not produce; the persisted metadata is the ground truth.)

```bash
./bin/persatrix channel history planning --json \
  | jq -r '.[] | "\(.sender_id)\t\(.metadata.interaction_id)"'
```

**Verification**:
- [ ] Both participants reply; the history shows one shared
  `interaction_id` across the whole discussion — no row missing it.

### Step 2: The discussion converges on votes

Let the round continue (a follow-up "any objections?" from `alex` is fine if
the personas stall). Within a few turns the personas should agree and emit
votes — visible in the timeline as short sign-off messages ("Nothing further
from me…").

**Expected**:
- At least two distinct personas emit vote-shaped sign-offs.
- On the second distinct vote (within W=3 turns of the first), the
  orchestrator logs `channels: interaction closed by end-of-interaction
  votes` and emits `interaction_closed{trigger=end_votes}`.
- **No further persona replies** appear after the close — the conversation
  stopped because its participants said so, well before five cascade hops.
- `governance_drop{layer=depth}` stays **zero** for the whole arc.

**Verification**:
- [ ] The close fires on the second distinct vote; the room then stays quiet.
- [ ] No depth-cap drop occurred (the semantic terminator beat the backstop).

### Step 3: The summary surface hands back the result

```bash
./bin/persatrix agent interactions iron-fox --limit 1
```

**Expected**:
- The closed interaction appears with the close trigger rendered as
  **"ended"** (the CLI's label for a `structural` close; add `--json` to
  see the literal `structural`) and a readable summary that names the
  decision (relay vs. beacon) — the converged outcome, not a blank or the
  `[interaction summary unavailable]` sentinel. The console's conversation
  view shows the "interaction closed" affordance below the turns.

**Verification**:
- [ ] The summary names the decision the personas converged on.

### Step 4: The room lives on

```bash
./bin/persatrix channel send planning \
  "New topic: what should we cover in Friday's retro?" \
  --as alex
```

**Expected**:
- The personas reply normally — the vote ended one conversation, not the
  channel. The channel history (same `jq` read as Step 1) shows the new
  traffic stamped with a **different** `interaction_id` than the closed
  discussion's.

**Verification**:
- [ ] Replies resume immediately on a fresh interaction.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Discussion proceeds; every publish stamped with one shared interaction id | ☐ |
| 2 | Two distinct votes close the interaction (`trigger=end_votes`); no further replies; zero depth-cap drops | ☐ |
| 3 | The interaction-summary surface returns a readable converged outcome | ☐ |
| 4 | A new topic draws replies on a fresh interaction — the channel is not dead | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Personas discuss but never vote

The prompt-half of Layer 4 is judgement, not mechanism — a persona may keep
finding things to add. That is not a failure of this MT's machinery: idle
rotation (`interaction_idle_timeout_seconds`, default 600s) is the net.
Note the emission is **lazy**: a quiet room emits nothing — the
`interaction_closed{trigger=idle}` fires on the channel's **next publish**
after the window has passed, when the resolver rotates to a fresh
interaction (see the [channels guide](../guides/channels.md)'s idle-rotation
caveat). If votes *never*
occur across several closable prompts, treat it as prompt-snippet
calibration feedback (the vote bar may be set too high), not a wire defect —
the deterministic close path is pinned by automation either way.

### Edge Case 2: A vote in a DM

Prompted or not, a DM vote must not close anything: the agent-side producer
drops it (`status=dm_channel`) before it reaches the wire. Verify by asking
a persona in a DM to "wrap up" — no `interaction_closed` may result from DM
traffic.

---

## Test Results

| Date | Tester | Build | Result | Notes |
|------|--------|-------|--------|-------|
|      |        |       |        |       |

## Notes

- The votes are real messages in channel history — they survive as the
  conversation's visible ending, which is the design (an audit trail of who
  judged it done).
