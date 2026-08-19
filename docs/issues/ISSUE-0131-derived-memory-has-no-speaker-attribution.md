---
id: ISSUE-0131
summary: "Memory derived at interaction close records WHAT was said and WHERE, never WHO said it. A `Fact` carries `subject` / `source_channel_id` / `source_interaction_id` (`agents/memory/fact_types.py`) but no speaker; the interaction record it is extracted from is room-scoped (`scope_for_channel_event` returns `group:<channel_id>` — `sender_id` is only the legacy-chat fallback). The principal axis does not substitute: emission is `auth.mode: enabled`-only and agent publishes are unauthenticated, so in a multi-agent room every persona turn collapses into the one shared `local` tenant. Consequences: hearsay is stored indistinguishably from first-hand testimony (agent B's restatement of A becomes a fact with A's authority — the confabulation-laundering path in an A→A→A cascade), and the persona cannot ground `you told me` against `Bob told me` in a shared room. Sibling of the ISSUE-0082 R-1 residual: R-1 splits the close record by PRINCIPAL, which leaves every unauthenticated speaker in one bucket; the speaker axis is the half R-1 does not cover."
status: open
severity: medium
area: memory
created: 2026-08-19
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
  `group:<channel_id>` / `thread:<thread_id>`; `sender_id` participates
  only as the final legacy-chat fallback. So a group room accumulates one
  `InteractionTracker` record across every speaker, and
  `summarize_close.py` summarises and extracts from that aggregate.
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

The open design question is whether the speaker joins the record KEY
(one close record per speaker) or stays a COLUMN on the derived rows
(one record per room-or-principal, rows attributed individually). That
question is the same one the ISSUE-0082 R-1 Phase 0 gate already exists
to answer, and it should be answered once, for both — a key-side answer
multiplies the RFC 0052 close-summary reserve a second time, and
under-sizing that reserve degrades silently into `budget_denied`
placeholders rather than failing loudly.

## Notes

> 2026-08-19 — filed while considering pre-v0.4.0 scope across the
> conversational topologies (human→persona→human, agent→agent→agent,
> and the delegate shapes above them). Slotted **v0.3.15** by the
> [sequencing Amendment 2026-08-19](../v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train)
> so it rides the same `messages` migration and the same record-shape
> decision as the ISSUE-0082 residuals rather than re-opening both later.
