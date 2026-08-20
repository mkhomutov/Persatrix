# ISSUE-0082 residuals — the Phase 0 design gate (record)

**Companion to**: [ISSUE-0082 residuals PR plan](ISSUE-0082-residuals-pr-plan.md)
**Issues**: [ISSUE-0123](ISSUE-0123-per-speaker-interaction-scope.md) (R-1) · `ISSUE-0131` (the speaker axis)
**Status**: ✅ Both axes resolved — Phase 0 on 2026-08-07, Phase 0b on 2026-08-21

This is the *evidence record* for the record-shape decision the residuals
workstream turns on: how the call was made, on what measurement, and what it
cost. It is split out of the PR plan so the plan stays a plan — and so the
gate can be cited on its own, which the sequencing Amendment 2026-08-19
does ([v0.3.x-sequencing.md](../v0.3.x-sequencing.md) — that amendment lands
with [#839](https://github.com/mkhomutov/Persatrix/pull/839), so this link is
file-level until it is on `main`).

**The answer, in one line**: the `InteractionTracker` is keyed
`(principal, speaker, scope)` — Phase 0 fixed the principal dimension,
Phase 0b the speaker dimension, both off the same live run.

---

## Phase 0 — the design gate ✅ RESOLVED 2026-08-07: **Option A**

> **Ran, and it decided.** The gate below is kept as the record of *how* the
> call was made. The measurement came back positive on **both** halves of the
> decision rule, so Option B is out and PRs 3–4 stand as written.
>
> Live on Anthropic, `group:planning`, three personas + one human, real close
> (`close_reason: idle_gap`). The `turn_count=2` summary on both iron-fox and
> nova-sparrow reads: *"Alice requested coverage … **due to her daughter Mira's
> surgery**. **Ember-owl proposed** three options: delegating to Iron Fox
> (strong on production reliability) …"* — one aggregate, one principal, a
> named human's personal disclosure plus an attributed second speaker's
> contribution. Not generic; the episode is a real leak vector.
>
> The facts tier is worse than ISSUE-0123 assumed: all three personas
> extracted `alice / has_child_named / Mira` (cross-room by default, RFC 0049
> Phase 1), and nova-sparrow extracted `iron fox / self.has_attribute / strong
> on production reliability` — **which iron-fox never said**; Ember-owl did.
> A single close writes third-party attributes derived from a second party's
> turn. Per-principal extraction over a shared record would not bound that;
> splitting the record does.
>
> **Caveat**: the run had `auth.mode: disabled`, so every row reads
> `principal_id='local'` and the principal-partitioning half is unevidenced.
> It does not need to be for this call — the tracker keys on the room scope
> regardless of principal, so the content aggregation measured here is
> auth-independent. The principal half stays MT Leg 4 under `enabled`, a
> release-prep deliverable.

**R-1's shape was deliberately NOT locked at plan authoring.** [ISSUE-0123](ISSUE-0123-per-speaker-interaction-scope.md) proposes per-speaker records; that was one of three answers, and the choice turned on evidence nobody had.

**Action**: run [MT-MEMORY-GROUP-TENANT-001](../manual-tests/MT-MEMORY-GROUP-TENANT-001.md) Legs 1–4 and read *where the leak concentrates*.

| Option | Shape | Cost | Residual |
|---|---|---|---|
| **A — per-speaker records** | Tracker keyed `(principal, scope)`; N records, N summaries, N extractions | `1 + (personas × principals)` close summaries | Persona's memory of a group discussion becomes N partial views |
| **B — room record, per-principal extraction** | One room `Interaction` and one summary; RFC 0026 extraction runs once per principal over that principal's turn subset | One summary + N extractions | The episode summary still narrates everyone under one principal |
| **C — B + episode to `'local'`** | As B, with the group episode written to the shared tenant | Same as B | A person's own group-room episode summaries are unreachable from their authenticated turns |

**Decision rule.** Read the Leg 4 `turn_count > 1` episode summary alongside the extracted facts:

* If the **summary text** materially carries another speaker's disclosure — not just "the team discussed scheduling" — the episode is a real leak vector and **Option A** is required.
* If the leak is concentrated in `facts` while the summary stays generic, **Option B** closes the actual cross-room vector (facts are cross-room by default per [RFC 0049](../rfcs/0049-memory-consolidation-gradient.md) Phase 1) at a fraction of the cost, and the episode residual is stated rather than paid for.
* **Option C** only if B's episode residual is judged unacceptable *and* the recall loss is judged cheaper than A's cost.

Do **not** attribute facts to speakers by asking the model. Per-turn membership is structural; LLM-elected attribution is not a boundary, and this repo does not ship model-elected boundaries.

**Output**: a dated scope-lock note appended to ISSUE-0123, naming the option and the evidence. PRs 3–4 below are written for Option A and are re-scoped if the gate selects B or C.

---

## Phase 0b — the speaker axis ✅ RESOLVED 2026-08-21: **speaker joins the key**

> **Same gate, second axis, no new measurement.** Phase 0 locked the shape on the
> *principal* axis. `ISSUE-0131` asks whether the SPEAKER is a second key
> dimension or a column on the derived rows — and the evidence above already
> answers it, because that run's decisive leak was agent-to-agent.

**Option A does not cover it.** Option A keys `(principal, scope)`. The leak the gate called decisive — nova-sparrow extracting `iron fox / self.has_attribute / strong on production reliability`, which **Ember-owl** asserted — is between two *personas*. Agent publishes re-enter unauthenticated (R-2), so both resolve to the same `local` principal and both turns land in one record. The misattribution that ruled out Option B survives Option A unchanged. The rule fired correctly; the shape it named is one dimension short.

**A column cannot substitute.** A `source_participant_id` stamped on a fact extracted from a multi-speaker aggregate could only be filled by asking the model which speaker it came from — which §Phase 0 already forbids: *"LLM-elected attribution is not a boundary, and this repo does not ship model-elected boundaries."* A speaker column is sound only once its record is already single-speaker. The column is a projection of the key, not an alternative.

**Decision.** Tracker keyed `(principal, speaker, scope)`; `Interaction.speaker_id` frozen at open beside `principal_id`; the close fan covers every `(principal, speaker)` record. The Phase 0 rule applied unchanged — *does the summary materially carry another speaker's disclosure?* — it does, between agents, so the record splits on that axis too.

| | Phase 0 (principal) | Phase 0b (speaker) |
|---|---|---|
| Answer | **Option A** — `(principal, scope)` | **key-side** — `(principal, speaker, scope)` |
| Evidence | 2026-08-07 live run | the same run's agent-to-agent misattribution |
| Bounds | one tenant's disclosure vs. another | one persona's assertion vs. another's |

**Cost, stated not discovered.** The close reserve goes from `1 + (personas × principals)` to `1 + (personas × principals × speakers)` — the largest cost in the workstream, and PR 4's to size. Under-sizing degrades **silently**: a denied lease commits `SUMMARY_UNAVAILABLE_TEXT` and the janitor never retries it.

**Residual, accepted.** Close-derived memory of a group discussion fragments per speaker per tenant. Room continuity is unaffected (transcript and RFC 0036 history are scoped by neither), but the release note must state the trade.
