---
id: ISSUE-0131
summary: "Memory derived at interaction close records WHAT was said and WHERE, never WHO said it. A `Fact` carries `subject` / `source_channel_id` / `source_interaction_id` (`agents/memory/fact_types.py`) but no speaker; the interaction record it is extracted from is room-scoped in exactly the topology that matters (`scope_for_channel_event` returns `group:<channel_id>` / `thread:<thread_id>`, neither carrying a speaker; the DM branch DOES key on `sender_id` via `scope_for_dm`, so a DM is already per-speaker and a shared room is where the speakers collapse). The principal axis does not substitute: emission is `auth.mode: enabled`-only and agent publishes are unauthenticated, so in a multi-agent room every persona turn collapses into the one shared `local` tenant. Consequences: hearsay is stored indistinguishably from first-hand testimony (agent B's restatement of A becomes a fact with A's authority — the confabulation-laundering path in an A→A→A cascade), and the persona cannot ground `you told me` against `Bob told me` in a shared room. Sibling of the ISSUE-0082 R-1 residual: R-1 splits the close record by PRINCIPAL, which leaves every unauthenticated speaker in one bucket; the speaker axis is the half R-1 does not cover."
status: resolved
severity: medium
area: memory
created: 2026-08-19
closed: 2026-09-03
closed_pr: 855
refs:
  - docs/issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md
  - docs/issues/ISSUE-0130-catchup-replay-rederives-memory-under-default-principal.md
  - docs/rfcs/0020-interaction-lifecycle.md
  - docs/rfcs/0026-declarative-facts-tier.md
  - docs/rfcs/0049-memory-consolidation-gradient.md
  - agents/memory/fact_types.py
  - agents/memory/scopes.py
  - agents/persona_runtime/summarize_close.py
  - internal/channels/sqlite_schema.go
---

## Summary

Derived memory has no speaker axis. Facts and close-time summaries record
the room and the subject, not the participant whose turn produced them.

## Context

Three independent places make the same omission:

- **The fact row.** `Fact` (`agents/memory/fact_types.py`) carries
  `subject`, `predicate`, `object`, `source_interaction_id`,
  `source_channel_id`, `protection_level`, `session_id` — and no speaker.
  `subject` answers *who the fact is about*, which is a different
  question from *who asserted it*.
- **The interaction record it is extracted from.** For a channel event
  `scope_for_channel_event` (`agents/memory/scopes.py`) resolves
  `group:<channel_id>` / `thread:<thread_id>` — neither carries a
  speaker. The DM branch is the instructive exception: both the
  `channel_type == "dm"` route and the `dm:` prefix route return
  `scope_for_dm(local_agent_id, sender_id)`, so a DM scope is **already
  keyed per-speaker** on the primary path. (`sender_id` is *also* the
  final fallback for a legacy-chat event with no `channel_id` and no
  `thread_id`, but that is not its only role.) A group room therefore
  accumulates one `InteractionTracker` record across every speaker, and
  `summarize_close.py` summarises and extracts from that aggregate.
  Worth carrying into the record-shape decision below: one topology
  already answers key-vs-column in the key's favour.
- **The tenant axis does not stand in for it.** Principal emission is
  `auth.mode: enabled`-only and a persona's outbound publish re-enters
  unauthenticated (ISSUE-0082 R-2), so every agent turn — and every turn
  under `auth.mode: disabled` — resolves to the shared `local` principal.

The message row itself is no help either: `messages` has no principal
column (that is ISSUE-0130 shape (b)) and its `sender_id` is not
projected onto anything derived from it.

## Impact

- **Hearsay is stored as testimony.** In an A→A→A cascade, persona B's
  restatement of what A said is extracted as a first-class fact with no
  marker distinguishing it from A's own assertion. Facts are cross-room
  by default since RFC 0049 Phase 1, so the restatement then travels.
- **The persona cannot attribute what it knows.** "You told me" versus
  "Bob told me in the standup" is not derivable from the stored row, in
  exactly the topology (a shared room with several people) where getting
  it wrong is most visible.
- **ISSUE-0082 R-1 does not close it.** R-1 keys the close record by
  principal. That is the right split for two authenticated people, and it
  leaves every unauthenticated speaker — the whole persona fleet — in one
  bucket.
- **v0.4.0 depends on it.** RFC 0012 authority and RFC 0028 deliberation
  both need "who proposed / who objected"; an unattributed record cannot
  answer either.

## Proposed fix / investigation path

Carry the speaker onto derived rows: a `source_participant_id` (plus the
RFC 0011 `participant_type` already on the wire) stamped at extraction
from the turn that produced the content, and surfaced on recall so the
render can attribute.

Note where that lands. The derived rows live in the **Python
persona-memory store** (`agents/memory/`, currently at migration 17), so
this is its own migration 17 → 18 — *not* the channel-store change
ISSUE-0130 shape (b) makes to `messages` (Go,
`internal/channels/sqlite_schema.go`, v11 → v12). The two stores are
disjoint: `agents/` issues no query against `messages`. What the two
issues share is the record-shape decision below, not a schema.

The open design question is whether the speaker joins the record KEY
(one close record per speaker) or stays a COLUMN on the derived rows
(one record per room-or-principal, rows attributed individually). That
question is the same one the ISSUE-0082 R-1 Phase 0 gate already exists
to answer, and it should be answered once, for both — a key-side answer
multiplies the RFC 0052 close-summary reserve a second time, and
under-sizing that reserve degrades silently into
`"[interaction summary unavailable]"` placeholders
(`SUMMARY_UNAVAILABLE_TEXT`, `agents/memory/interaction_janitor.py`)
rather than failing loudly.

## Notes

> 2026-08-19 — filed while considering pre-v0.4.0 scope across the
> conversational topologies (human→persona→human, agent→agent→agent,
> and the delegate shapes above them). Slotted **v0.3.15** by the
> [sequencing Amendment 2026-08-19](../v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train)
> so it rides the same record-shape decision as the ISSUE-0082 residuals
> rather than re-opening it later. It carries its own memory-store
> migration (17 → 18); the `messages` v11 → v12 change belongs to
> ISSUE-0130 shape (b) alone, in the other store.
>
> 2026-08-23 — **Boarded v0.3.15**, by the [v0.3.15 plan](../v0.3.15-plan.md)
> (Phase 0). The speaker axis is not re-designed at the plan opening: the
> [Phase 0 gate](ISSUE-0082-residuals-phase0-gate.md) resolved it 2026-08-21
> **key-side** — the `InteractionTracker` is keyed `(principal, speaker,
> scope)` — on the same 2026-08-07 live evidence that fixed the principal
> dimension, and the plan records that answer rather than re-deriving it. The
> work ships inside the [residuals PR plan](ISSUE-0082-residuals-pr-plan.md)
> (PR 3 installs the key and persona-memory migration 17 → 18; PR 4 binds the
> close path and re-sizes the reserve), which the milestone plan delegates to
> whole.
>
> Two obligations the milestone attaches. The reserve calibration filed at
> PR 4 must cover the **half-cap clamp** in
> [`internal/wallet/synthesis_reserve.go`](../../internal/wallet/synthesis_reserve.go),
> not only the `personas × principals × speakers` multiplier — a third
> dimension makes that clamp bite in the normal case rather than the edge case,
> and under-sizing degrades silently into `SUMMARY_UNAVAILABLE_TEXT`. And the
> release gate is the *extended* [MT-MEMORY-GROUP-TENANT-001](../manual-tests/MT-MEMORY-GROUP-TENANT-001.md),
> so residuals PR 5 does not run its arc before the
> [ISSUE-0130](ISSUE-0130-catchup-replay-rederives-memory-under-default-principal.md)(b)
> restart leg lands.
>
> Restated as a milestone scope lock, since it is the standing temptation
> here: **model-elected attribution stays forbidden.** The speaker column is a
> projection of the record key; it is sound only once the record is already
> single-speaker.

> 2026-08-26 — **the axis landed** (v0.3.15 residuals **PR 3**, this PR), in the
> key-side shape Phase 0b decided. `Interaction.speaker_id` is resolved from the
> triggering event's `sender_id` and frozen at open, and it is half the tracker
> key — so the Phase 0b regression case (one `local` principal, three agent
> speakers, one room scope) now yields **three** records rather than the single
> aggregate plain Option A would have shipped; that case is pinned as a test.
> Persona-memory migration **17 → 18** adds a nullable `speaker_id` to
> `episodes` and `facts` — the two tiers a group close writes, and not the two
> nearby wrong targets this issue's proposal warned about. It lands **dormant**:
> the close-path binding that stamps it is PR 4, so the column ships ahead of
> its consumer (the v0.3.15 "no migration lands after its consumer" acceptance
> line) and pre-existing rows stay NULL. No backfill and no guess — a pre-split
> aggregate's speaker is unknowable without exactly the model-elected
> attribution the scope lock forbids, which is also why the column is only sound
> now that the record it projects from is single-speaker by construction.

> **Resolved 2026-09-03 — the speaker axis verified live** ([v0.3.15 execution report](../manual-tests/v0.3.15-execution-report.md), Leg 4).
> Every persona held one record **per speaker it heard** and none for itself:
> 2 / 3 / 2 records across the fleet, each single-speaker and
> single-principal. Pre-fix this is one merged record per persona aggregating
> every speaker. The axis reaches the extracted facts too — e.g.
> `(alice-person, nova-sparrow, ember, agreed_to, cover review slot)`.
>
> **Auth-independent, as designed**: under `auth.mode: disabled` (Leg 8) the
> `local` partition still held **two** distinct speakers where v0.3.14 closed
> one. The split is a property of the record key, not of authentication.
