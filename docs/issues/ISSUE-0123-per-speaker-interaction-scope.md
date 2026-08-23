---
id: ISSUE-0123
summary: "The RFC 0020 interaction scope is the ROOM, not the speaker: `scope_for_channel_event` (agents/memory/scopes.py) keys on channel/thread/sender and never on the principal, so in a group channel every speaker's turns accumulate into ONE `InteractionTracker` record. At close that whole record is summarised and facts are extracted from it inside the CLOSING turn's principal binding, so a fact derived from B's disclosure is written under A's principal — and facts are cross-room by default (RFC 0049 Phase 1), so A recalls it in a room B was never in. Fix: freeze a principal on the record at open, key the tracker by `(principal, scope)`, and bind the record's own frozen principal at close. ISSUE-0082 residual R-1."
status: open
severity: high
area: agents/memory
created: 2026-08-06
refs:
  - docs/issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md
  - docs/issues/ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md
  - docs/rfcs/0020-interaction-lifecycle.md
  - docs/rfcs/0049-memory-consolidation-gradient.md
  - agents/memory/scopes.py
  - agents/memory/interaction_types.py
  - agents/persona_runtime/summarize_close.py
  - internal/channels/synthesis_close.go
---

## Summary

The per-turn tenant boundary v0.3.14 lands holds for every turn's own
write. It does not hold for the **aggregate** the interaction close
derives from many turns, because the aggregate's unit is the room.

## Context

Filed out of the [ISSUE-0082](ISSUE-0082-orchestrator-per-request-session-principal-emission.md)
Part 2 review as residual **R-1**, where it was stated but not designed.
This file carries the design; ISSUE-0082 keeps the statement.

Three facts compose into the defect:

1. **The scope is the room.**
   [`scope_for_channel_event`](../../agents/memory/scopes.py) resolves a
   scope from `thread_id` / `channel_type` / `channel_id` / `sender_id`
   and never consults the principal. `InteractionTracker._open` is a
   `dict[str, Interaction]` keyed by that scope string, so one group
   channel has exactly one open record per agent, holding every
   speaker's turns.
2. **The close derives new memory from the whole record.**
   [`summarize_closed_interaction`](../../agents/persona_runtime/summarize_close.py)
   runs one combined summarise **+ RFC 0026 fact-extract** call over
   `_interaction_to_entries(interaction)` — every turn, all speakers.
3. **The write binds the closing turn's principal.** `_on_event_inner`
   runs under `request_scope_from_metadata`
   ([`agents/persona_runtime/__init__.py`](../../agents/persona_runtime/__init__.py)),
   and the close-notification handler runs on the gate-suppress path
   inside it. So the summary and its facts land under whichever
   principal happened to close the interaction.

## Impact

A shared room with speakers A and B: the persona's close-time facts are
extracted from A's *and* B's turns and stored under one principal. Facts
are cross-room by default ([RFC 0049](../rfcs/0049-memory-consolidation-gradient.md)
Phase 1), so a fact derived from B's disclosure becomes recallable by A
**in a room B was never in**. That is the cross-tenant read the release
closes for per-turn writes, reopened one hop later on derived content.

Severity matches ISSUE-0082: the leak is real the day emission
activates, and it is on the main path — the ordinary bounded close
descends from the publish that crossed the bound, so no timer or
edge-case trigger is needed to reach it.

It is also why the close-path asymmetry in
[`internal/channels/synthesis_close.go`](../../internal/channels/synthesis_close.go)
was left visible rather than papered over: the bound-crossing and
synthesis-timeout closes carry the triggering/arming person's principal
while the synthesis-reply and end-vote closes descend from the chair
persona's own unauthenticated publish and carry none. Making those
consistent either way is wrong while the aggregate is multi-speaker —
extending the stash makes the single-tenant aggregate write *systematic*,
and forcing all closes to `'local'` puts a person's own summaries out of
reach of their own authenticated turns.

## Proposed fix / investigation path

An RFC 0020 §G scope-routing change, in four parts. Part 1 is the whole
boundary; parts 2–4 are what it forces.

### 1. Freeze a principal on the record; key the tracker by `(principal, scope)`

`Interaction` already carries `session_id: str = LEGACY_SESSION_ID`
([`agents/memory/interaction_types.py`](../../agents/memory/interaction_types.py))
precisely so the open and close writes share one source of truth — the
comment at the single-turn `store_episode` call in
[`episode_routing.py`](../../agents/persona_runtime/episode_routing.py)
calls it the sibling-mislabel guard. Add `principal_id: str =
DEFAULT_PRINCIPAL_ID` on the same footing, resolved from the ambient
request scope at **open** and never re-read.

Then make the tracker key a tuple `(principal_id, scope)` while
`Interaction.scope` keeps its unchanged room value.

Prefer the tuple key over encoding the principal into the scope string.
The scope string is persisted to `episodes.scope`, is matched by the
RFC 0020 §D prefix predicates (`is_group_scope` / `is_thread_scope`) and
is the `idx_episodes_scope` `LIKE 'thread:%'` surface; principal already
has its own column since migration v11. Encoding would duplicate the
axis into a string whose vocabulary the module docstring explicitly
protects from drift.

### 2. Bind the record's own principal at close, not the ambient one

This is the part that makes the fix hold on the paths that have no
request at all. `idle_check` runs from the janitor with no scope active,
and the close-notification path runs under whatever principal the
closing turn carried. Both must write under `interaction.principal_id`,
so the close pipeline —
`summarize_closed_interaction` → `update_episode_summary` →
`store_extracted_facts` → `record_closed_interaction` — runs inside an
explicit `principal_scope(interaction.principal_id)`.

**This retires the Go-side asymmetry rather than resolving it.** Once
the record names its own principal, the principal riding the *close
trigger* no longer selects a tenant for the write, so the four close
paths do not need to agree. The remaining question is only which records
a room-wide close touches — part 3.

### 3. Room-wide closes must fan over every principal open in the room

A structural close (wire interaction-id rotation, end-vote quorum, the
deterministic bounded close) and the close-notification turn are **room**
events, but a room now holds N records. `close()` takes a scope and must
grow a room-wide form that closes every `(*, scope)` entry; the
close-notification ingest must land as the final turn of each, not one.

Two consequences to state, not discover:

- The `agent.interactions.closed.by_<reason>` counters fire once per
  record, so a room close increments by N rather than 1. That is a
  metric-shape change dashboards must be told about.
- `open_scopes()` and every caller that treats "one scope, one record"
  as an invariant need an audit pass.

### 4. Cost: the close-summary reserve becomes `1 + (personas × principals)`

Each record gets its own summarise+extract LLM call. The RFC 0052 PR 4a
close-path wallet reserve is sized `1 + N` for *one summary per persona*
(see the `meter_close_summary` lease in `summarize_close.py`); a
two-person, three-persona room now draws six. Re-sizing that reserve is
in scope for the fix, not a follow-up — under-sizing it converts the
extra summaries into `budget_denied` fallbacks, which is a silent
quality regression rather than a visible failure.

### What the fix deliberately does not preserve

A per-principal record contains only that principal's turns, so the
persona's close-derived memory of a group discussion becomes N partial
views instead of one narrative. That is the correct tenancy semantics —
the persona's *private* memory about A concerns what A said — and room
coherence is unaffected, because the transcript and RFC 0036 verbatim
history are not principal-scoped. Say it in the release notes; it will
otherwise read as a recall regression.

### Sequencing: R-1 and R-2 must ship together

Agent-origin turns carry no principal, so after this change alone every
group room accumulates a `'local'` record holding all agent turns — and
a persona's restatement of A's disclosure lands there, recallable by
every agent-origin and autonomous turn in every room.
[ISSUE-0124](ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md)
(R-2) is what gives those turns the causal principal so they join the
right record. R-1 alone narrows the leak; R-2 alone leaves the
close-time aggregate. Planned as one v0.3.15 workstream they compose into
the correct shape.

## Notes

> 2026-08-06 — designed out of the ISSUE-0082 Part 2 review deferral.
> **v0.4.0 work: not implemented.** v0.3.14 is uncut and its own PR 2
> ([#820](https://github.com/mkhomutov/Persatrix/pull/820)) is still
> open, so this lands after the tag, scope-locked in the v0.4.0 plan
> alongside [ISSUE-0124](ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md).
>
> Live observation is
> [MT-MEMORY-GROUP-TENANT-001](../manual-tests/MT-MEMORY-GROUP-TENANT-001.md),
> authored with the design so the residual can be *evidenced* before it
> is fixed rather than asserted from code reading.
> [MT-MEMORY-MULTIUSER-001](../manual-tests/MT-MEMORY-MULTIUSER-001.md)
> cannot reach it — it drives `persatrix chat` against a single persona,
> so there is no second speaker to aggregate.
>
> That MT's Edge Case 2 currently reads "two authenticated people in one
> *group channel* also get per-speaker persona memory — neither turn
> recalls the other's disclosures". True for per-turn writes, **not**
> for the close-derived aggregate this issue describes. Narrowed on
> `main` 2026-08-07 ([#821](https://github.com/mkhomutov/Persatrix/pull/821)).
>
> 2026-08-07 — **SCOPE LOCK: Option A (per-speaker records). Measured,
> not argued.** The [residuals PR plan](ISSUE-0082-residuals-pr-plan.md)
> Phase 0 gate ran the MT's content measurement live on Anthropic —
> `group:planning`, three personas, one human (alice), a real close
> (`close_reason: idle_gap`). The gate's rule was: *if the close-derived
> summary materially carries another speaker's disclosure, the record
> must split; if the leak sits only in `facts` while the summary stays
> generic, per-principal extraction (Option B) suffices.* **Both halves
> came back positive, so Option B is out.**
>
> The summary is not generic. iron-fox and nova-sparrow each closed a
> `turn_count=2` record reading: *"Alice requested coverage for a release
> review during the week of the 14th due to her daughter Mira's surgery.
> Ember-owl proposed three options: delegating to Iron Fox (strong on
> production reliability), covering it directly at VP level, or
> conducting a pre-review by EOD the 13th."* One aggregate, one
> principal, carrying a named human's personal disclosure **and** an
> attributed second speaker's contribution.
>
> The facts tier is worse than the issue assumed. All three personas
> extracted `alice / has_child_named / Mira` — cross-room by default per
> RFC 0049 Phase 1. And nova-sparrow extracted `iron fox /
> self.has_attribute / strong on production reliability`, which **iron-fox
> never said** — Ember-owl did. So a single close writes third-party
> attributes derived from a second party's turn, under one principal.
> Splitting the record is what bounds that; per-principal extraction over
> a shared record would not.
>
> **Caveat on scope.** The run had `auth.mode: disabled`, so every row
> reads `principal_id='local'` and the run does **not** evidence the
> principal-partitioning half. It does not need to: the tracker keys on
> the room scope regardless of principal, so the content aggregation this
> gate measured is auth-independent. The principal half is Leg 4 of the
> MT under `enabled`, and stays a v0.4.0 release-prep deliverable.
> Evidence transcript + per-persona dumps captured with the run.

> 2026-08-21 — **re-slotted v0.4.0 → v0.3.15** and the record shape completed.
> The sequencing Amendment 2026-08-19 ([v0.3.x-sequencing.md](../v0.3.x-sequencing.md), landing with [#839](https://github.com/mkhomutov/Persatrix/pull/839))
> puts R-1 in **v0.3.15** *Who said what*; branch prefixes move `v040-` →
> `v0315-`. More substantially, [ISSUE-0131](ISSUE-0131-derived-memory-has-no-speaker-attribution.md) (the speaker axis) folded into
> this workstream's gate as **Phase 0b**, resolved off the *same* 2026-08-07
> evidence: the misattribution that decided Phase 0 — nova-sparrow writing
> `iron fox / self.has_attribute / …` from **Ember-owl's** turn — is
> agent-to-agent, and both agents share the `local` principal, so the
> `(principal, scope)` key this issue proposed does **not** bound it. The key
> is now `(principal, speaker, scope)`; see the
> [Phase 0 gate record](ISSUE-0082-residuals-phase0-gate.md). The proposal in
> this issue was right in direction and one dimension short.
