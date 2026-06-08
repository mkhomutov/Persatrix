# Manual Test MT-CHANNEL-RELEVANCE-002: Relevance gate Tier B — salience bid, `chair`, NL-addressing

**Test ID**: `MT-CHANNEL-RELEVANCE-002`
**Feature Area**: Channels (conversation governance — RFC 0030 Layer 3, relevance amendment **Tier B**)
**Version**: 1.0
**Created**: 2026-06-08
**Last Updated**: 2026-06-08
**Status**: Active

---

## Overview

**Purpose**: Verify the v0.3.8 user-facing promise of **no pile-on** (RFC 0030
Layer 3, the [relevance-gated-response amendment](../rfcs/0030-amendment-relevance-gated-response.md)
**Tier B**): on an **open-floor** message, an open-floor member runs a cheap
`fast`-model **salience bid** and stays **silent** unless it has something
genuinely new to add. Where [MT-CHANNEL-RELEVANCE-001](MT-CHANNEL-RELEVANCE-001.md)
(Tier A) fixed *addressing* — a directed `@`-mention is answered by exactly that
persona — Tier A still admitted **every** `participant` on an un-addressed
message. Tier B closes the remaining gap: an un-addressed question now draws a
**small relevant set**, not a pile-on, and a redundant follow-up draws
**silence**. This MT also exercises the two Tier B surfaces that ride with it:

- the **`chair`** disposition — a low-threshold facilitator that clears the bid
  readily and keeps the discussion moving, **but cannot close the conversation**
  (TB5); and
- **natural-language addressing** — a free-text "let's hear from X" *biases* the
  bid toward X without hard-dropping anyone (it is a signal, not a filter).

**Scope**: the default `planning` group channel, edited for this test to a
three-open-floor-member shape with one `chair` (see Preconditions), one human
prompt per case, and the Tier B salience bid the persona runtime runs in
[`agents/salience_bid.py`](../../agents/salience_bid.py) via the action-loop
seam [`agents/persona_runtime/salience_gate.py`](../../agents/persona_runtime/salience_gate.py).
Observed through the embedded web console Channel-timeline panel with a REST
cross-check for the per-sender reply counts, and the `GET /api/v1/cost/summary`
surface for the bid-is-leased check.

**Out of Scope** — covered by sibling MTs or deferred:

- **Addressing-aware directedness** (a `@`-mention answered by exactly one
  persona) — that is Tier A, asserted by
  [MT-CHANNEL-RELEVANCE-001](MT-CHANNEL-RELEVANCE-001.md).
- **Termination / convergence** (cost ceiling, reply budget, end-of-interaction
  vote) — the deterministic governance Layers 1/2/4, asserted by the
  `MT-CONVERSATION-CONVERGENCE-001` combined story. A `chair` does **not**
  terminate; this MT records that explicitly (Step 5).
- **The Layer 5 moderator** (the `chair`'s active half — a persona that reads the
  transcript and decides to wrap up / terminate) — **v0.4.0**; its seam is wired
  but inert in v0.3.8 ([`agents/chair_moderation.py`](../../agents/chair_moderation.py)).

---

## Related Documentation

- [RFC 0030 Amendment — Relevance-Gated Response](../rfcs/0030-amendment-relevance-gated-response.md) — design; Tier B is the v0.3.8 slice (the salience bid + `chair` + NL-addressing)
- [Tier B PR plan](../rfcs/0030-amendment-relevance-gated-response-tierb-pr-plan.md) — this MT is PR 4's acceptance test
- [docs/guides/channels.md §2 "Per-membership `respond` dispositions"](../guides/channels.md#per-membership-respond-dispositions) — the disposition vocabulary, the salience bid, `threshold`, `chair`, and `salience_max_channel_members`
- [docs/guides/persona-agents.md](../guides/persona-agents.md) — the persona-side "whether the persona speaks on the open floor" note

**Related Automated Tests**:
- [`tests/unit/python/test_salience_bid.py`](../../tests/unit/python/test_salience_bid.py) — bias-to-silence on a redundant point; speak on an in-domain unaddressed point; unset threshold → silence; lease denial / parse failure → silence (fail-closed)
- [`tests/unit/python/test_chair_moderation.py`](../../tests/unit/python/test_chair_moderation.py) — a `chair`'s low threshold clears where a default `participant` stays silent; the Layer-5 moderation seam is inert (always `CONTINUE`) and uninvoked (a `chair` **cannot** close — TB5)
- [`tests/unit/python/test_salience_addressing.py`](../../tests/unit/python/test_salience_addressing.py) — NL addressing biases the bid bar without a hard drop
- [`tests/integration/test_salience_action_loop.py`](../../tests/integration/test_salience_action_loop.py) — the seam fires only on a governed open-floor admit; an oversized channel falls back to `addressed`-only

---

## Preconditions

Same as [MT-CHANNEL-RELEVANCE-001 § Preconditions](MT-CHANNEL-RELEVANCE-001.md#preconditions)
(a valid API key in `.env` — the persona replies are real LLM calls; all demo
personas up on the default compose stack; orchestrator started with
`--enable-ui`; clean state via `make reset` or a fresh `PERSATRIX_EPOCH`),
**plus** a small edit to make the `planning` channel a three-open-floor-member
room with one `chair` (the default ships `ember-owl: addressed`, so only two
members reach the open floor — too few to show pile-on):

```yaml
# config/channels.yaml — planning.members
members:
  - id: ember-owl
    respond: participant        # was: addressed
  - id: iron-fox
    respond: participant
  - id: nova-sparrow
    respond: chair              # was: participant — the low-threshold facilitator
```

- ☐ `ember-owl` and `iron-fox` are `participant`; `nova-sparrow` is `chair`. No
  explicit `threshold` on the two `participant`s (so they bias to silence on
  ambiguous traffic, TB2); the `chair` inherits the low default.
- ☐ `salience_max_channel_members` is the default `20` (the channel has 3
  members, well under the cap — the bid runs; it is not skipped).
- ☐ State is clean (`make reset` or a fresh `PERSATRIX_EPOCH`).

Bring the stack up after the edit:

```bash
make reset
ENABLE_UI=1 docker compose up --build   # or `make run-ui` for the local-binary path
```

---

## Test Procedure

### Step 1: Confirm the channel's dispositions

**Action**: in the web console (`http://localhost:8080/ui`), open the
**Channels** panel and select **`group:planning`** (or via REST:
`Invoke-RestMethod http://127.0.0.1:8080/api/v1/channels/group:planning`).

**Expected**:
- `group:planning` lists all three members — `ember-owl`, `iron-fox`,
  `nova-sparrow` — and each shows **`respond: always`** on the REST surface. The
  disposition vocabulary is **normalized to the legacy triple at the store
  boundary** (`channels.RespondPolicy.Normalize`: `participant`/`chair` →
  `always`), so `participant` and `chair` are *both* indistinguishable from
  `always` in the membership listing. The chair's low `threshold` /
  `salience_gated` ride the config struct, **not** the membership row, and the
  member shape (`id` / `respond` / `joined_at`) does not surface them — so this
  endpoint **cannot** show the declared dispositions back. They are confirmed at
  config time (the Preconditions edit to `config/channels.yaml`) and
  **behaviourally** by Steps 2–5; Step 1 only confirms the three members are
  present and resolve to the open-floor (`always`) wire value.

**Verification**:
- [ ] All three members present; each resolves to `respond: always` on REST (the
  normalized open-floor value — the back-compat collapse). The
  `participant`/`chair` distinction is a config-time + behavioural check
  (Steps 2–5), not visible in the member listing.

---

### Step 2: An open-floor question draws a **small relevant set** (no pile-on)

Post a question into the room's domain but **not** `@`-addressed to anyone. Under
Tier A alone (v0.3.7), all three open-floor members would answer. Under Tier B,
each runs the salience bid and only those with something to add speak.

**Action** (the human sender must be a member first — join once, then send):

```bash
./bin/persatrix channel join planning --as operator
./bin/persatrix channel send planning \
  "Open question for the room: what's the single biggest risk in shipping the v0.4 event log?" \
  --as operator
```

**Expected**:
- **Fewer than three** persona replies — typically the `chair` (`nova-sparrow`,
  low threshold, clears readily) plus the one or two `participant`s whose lane
  the question is in. A `participant` with nothing distinctive to add stays
  silent (`reason="below_threshold"` / `declined`).
- No persona repeats a point another already made.

**Verification**:
- [ ] The open-floor question draws a **small set** (not all three); the `chair`
  is among those who speak.

---

### Step 3: A redundant follow-up draws **silence**

Immediately follow up with a message whose point the room has already covered.
The bid reads the in-round transcript and should decline ("already said").

**Action**:

```bash
./bin/persatrix channel send planning \
  "So it sounds like data-loss on the event log is the main risk — anything to add?" \
  --as operator
```

**Expected**:
- **Zero or at most one** persona reply. A `participant` whose point is already
  in the transcript stays silent (`below_threshold` / `declined`). At most the
  `chair` may add a brief facilitating nudge (its low threshold), but it does
  **not** restate covered ground.

**Verification**:
- [ ] The redundant follow-up draws silence (or only a brief `chair` nudge) — no
  pile-on of agreement.

---

### Step 4: Natural-language addressing biases toward the named persona

Post an open-floor message that names one persona in **free text** (no
`@`-mention). NL addressing *biases* the bid toward the named persona and away
from the others — a signal, not a hard filter.

**Action**:

```bash
./bin/persatrix channel send planning \
  "Good points. Let's hear from Iron Fox on the migration sequencing specifically." \
  --as operator
```

**Expected**:
- `iron-fox` speaks (the named persona — its bid bar is lowered).
- The others *defer* — they are biased toward silence, **not** hard-dropped: a
  persona with a decisively novel point could still clear. (Contrast Tier A,
  where a structured `@iron-fox` would suppress the others outright with
  `directed_elsewhere`.)

**Verification**:
- [ ] `iron-fox` replies; the others mostly defer (a non-named persona is biased
  toward silence but not deterministically suppressed).

---

### Step 5: The `chair` keeps things moving but **cannot close** the conversation

The `chair` facilitates readily (Steps 2–3) but has **no** power to terminate
the interaction — convergence is the job of Layers 1/2/4, not the chair. There
is no `chair`-driven close path in v0.3.8 (the Layer 5 moderator is v0.4.0; its
seam is inert).

**Action**: continue the conversation a few more open-floor turns and watch the
interaction state in the console (or `GET /api/v1/channels/group:planning` /
the interaction view).

**Expected**:
- The `chair` participates across turns but the interaction **does not close**
  on the chair's account — it stays open until a Layer 1/2/4 trigger (a budget
  ceiling, a reply-budget exhaustion, or K end-of-interaction votes) fires, or
  it idles out per RFC 0020. No message from the `chair` ends the interaction.

**Verification**:
- [ ] The `chair` never closes / terminates the interaction; the conversation
  ends only via a governance-layer trigger or idle timeout (this is the inert
  Layer-5 contract, TB5).

---

### Step 6: Confirm reply counts + that each bid is leased (cost attribution)

**Action**: pull the channel history newest-first and count distinct persona
senders per human message; cross-check that the bids leased on the `fast` alias.

```pwsh
(Invoke-RestMethod `
  "http://127.0.0.1:8080/api/v1/channels/group:planning/messages?limit=30") `
  | Select-Object timestamp, sender_id, @{n='content';e={$_.content.Substring(0,[Math]::Min(60,$_.content.Length))}} `
  | Format-Table -Auto

# Cost attribution: the salience bids show up as small `fast`-alias spend
# attributed to CAUSE_CHANNEL_MESSAGE — far cheaper than a full quality turn.
Invoke-RestMethod "http://127.0.0.1:8080/api/v1/cost/summary"
```

**Expected**:
- The open-floor question (Step 2) is followed by **fewer than three** persona
  messages; the redundant follow-up (Step 3) by **zero or one**; the NL-addressed
  message (Step 4) is led by `iron-fox`.
- The cost summary shows small `fast`-alias bid spend (each bid is leased and
  attributable, TB3) distinct from the larger `quality`-alias turn spend — and an
  **idle** persona that never bid costs zero.

**Verification**:
- [ ] Reply counts show no pile-on; the bid spend is small, leased, and
  attributable; idle personas cost zero.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | `group:planning` lists all three members; each resolves to `respond: always` on REST (dispositions normalized at the store boundary — the `chair` is not REST-distinguishable) | ☐ |
| 2 | An open-floor question draws a **small relevant set** (not all three); the `chair` speaks | ☐ |
| 3 | A redundant follow-up draws **silence** (or only a brief `chair` nudge) — no pile-on | ☐ |
| 4 | NL "let's hear from Iron Fox" biases toward `iron-fox`; others defer but are not hard-dropped | ☐ |
| 5 | The `chair` facilitates but **cannot close** the interaction (TB5; Layer-5 seam inert) | ☐ |
| 6 | REST shows no pile-on; the salience bid is small, leased (`fast`), attributable; idle = zero | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: A silent (out-bid) persona still **remembers**

Like a Tier-A `directed_elsewhere` member, a persona the salience bid kept silent
still **ingests** the message into its conversation window / memory — silence ≠
amnesia. A later turn that *does* draw it can build on what it saw. (Pinned by
the action-loop seam's `_store_event_episode` call on the suppress path.)

### Edge Case 2: A bare-`always` member is unaffected (back-compat)

A member written with the **literal `always`** keyword (not the `participant`
disposition) keeps replying unconditionally — it is **not** bid-governed unless
it carries an explicit `threshold`. So a config that never adopted the
disposition vocabulary behaves exactly as in v0.3.7. Re-point one member to
`always` to spot-check.

### Edge Case 3: Bid fails closed

If the `fast` alias is unconfigured, the lease is denied/exhausted, or the bid
output is unparseable, the member stays **silent** (`model_unresolvable` /
`lease_denied` / `parse_failure`) — the fail-closed direction (TB2). A `fast`
outage dampens the room rather than crashing it; the `reason` label distinguishes
it from genuine no-pile-on dampening on the `channel.messages.gated` counter.

### Edge Case 4: An oversized channel skips the bid (TB6)

Above `salience_max_channel_members` (default `20`) the bid is skipped and the
channel falls back to `addressed`-only, so an un-addressed open-floor
`participant` stays silent on a very large room. Lower the cap to verify the
fallback without standing up 20 personas.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| _pending_ | | | ☐ | To be executed live at v0.3.8 release-prep (master-plan Phase 3) on the RC tip, alongside `MT-CONVERSATION-CONVERGENCE-001`. |

---

## Notes

- The salience bid is **not** free (unlike Tier A): an open-floor admit on a
  governed channel pays one conversation-window fetch + one leased `fast` bid per
  message. The win is *not* paying the expensive half (memory recall + the
  `quality` turn) when the bid stays silent — the cheap-bid-vs-full-turn trade is
  the whole point. An **idle** persona (no inbound traffic) still costs zero (the
  RFC 0023/0024 idle-cost invariant).
- The deterministic guarantees (which members are admitted/suppressed, the
  fail-closed branches, the NL-addressing bar shift) are pinned by the unit +
  integration suites listed above; this MT validates the *conversational* effect
  end-to-end with live personas — that a brainstorm reads like a room that does
  not pile on.
- This MT pairs with `MT-CONVERSATION-CONVERGENCE-001`: Tier B (this MT) is the
  *no-pile-on* half; the governance layers are the *converge + terminate* half.
  Together they are the v0.3.8 "conversations that converge" story.
