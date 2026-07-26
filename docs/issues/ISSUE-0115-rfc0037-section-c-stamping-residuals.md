---
id: ISSUE-0115
summary: "Three RFC 0037 §C residuals left open by the PR 3 (memory substrate, #775) review, each owned by a later PR in the same plan but none currently scoped there: (a) §C promises operators a one-time flag to backfill all pre-migration notes at a chosen level, the escape hatch for the notes tier's documented under-protection residual — the notes backfill happens in PR 3's v16 column DEFAULT, and the flag appears in no PR's scope; (b) §C still specifies backfilling each entry from its recorded source channel's classification where resolvable, with the facts leg joining through the episode's interaction, while v16 ships a blanket `internal` DEFAULT — provably equivalent today (channel rows live in the orchestrator's channel store, unreachable from the memory DB, and channel-store v11 backfills every channel to `internal`), but the RFC text now describes a join that does not exist; (c) frozen-at-open capture means an interaction open across an UPWARD reclassification stamps the pre-raise level onto an episode/facts batch that includes post-raise turns — §C's read-once-per-interaction rule and the retroactive-reclassification non-goal cover the backfill case, not this live one, so the residual is currently undocumented rather than decided."
status: open
severity: medium
area: memory
created: 2026-07-26
refs:
  - docs/rfcs/0037-memory-confidentiality-channel-classification.md
  - docs/rfcs/0037-pr-plan.md
  - agents/memory/_migration_protection.py
  - agents/memory/interaction_types.py
  - agents/persona_runtime/close_path.py
  - agents/persona_runtime/fact_extractor.py
---

## Summary

Three §C residuals surfaced by the PR 3 review. None blocks the dark
substrate (nothing reads `protection_level` until the PR 4 §D gate, and the
PR 1 dark-window guard keeps every channel ≤ `internal` until the full
Phase-1 set ships), but each is either an RFC promise with no implementer or
a live behaviour the RFC does not describe.

## Context

Captured during the review of [PR #775](https://github.com/mkhomutov/Persatrix/pull/775)
(RFC 0037 PR 3 — memory substrate: migration v16, interaction-open capture,
close-consolidation stamping). Two review findings from the same pass — the
missing `agent_id` on `memory_projections` and the untested
wire→capture→stamp seam — were fixed in that PR; these three were left open
because they belong to later PRs in the [0037 PR plan](../rfcs/0037-pr-plan.md).

### (a) The notes-backfill operator flag has no implementer

[§C](../rfcs/0037-memory-confidentiality-channel-classification.md) documents
the notes tier as "the honest exception": notes carry no channel provenance,
so every pre-migration note backfills `internal` regardless of where it was
authored, including notes authored in `restricted`/`secret` turns. The RFC
offers an escape hatch for that accepted residual — *"operators with
sensitive histories may use a one-time flag to backfill all pre-migration
notes at a chosen level instead"*.

The notes backfill itself happens in PR 3, via the v16 `protection_level`
column DEFAULT (`agents/memory/_migration_protection.py`). The flag does
not exist, and `grep -i backfill docs/rfcs/0037-pr-plan.md` finds it in no
PR's scope — not PR 4's notes leg, not elsewhere. As written, the RFC reads
as though the affordance ships with the feature.

### (b) §C's backfill-by-join no longer describes the implementation

§C's *Migration backfill* paragraph specifies backfilling "each entry from
its recorded source channel's classification where resolvable (episodes
carry the RFC 0020 scope column; **facts carry only
`source_interaction_id`**, so the facts backfill joins through the episode's
interaction where possible)".

v16 implements a blanket `internal` DEFAULT with no join. The deviation is
**correct and better argued than the RFC**: channel classification lives in
the orchestrator's channel store, which the persona-memory database cannot
reach, and channel-store migration v11 backfills every existing channel to
`internal` — so the join would resolve `internal` for every row it could
resolve at all. But the RFC text still describes machinery that was never
built, which a future reader will take at face value when reasoning about
what pre-v0.3.12 rows mean.

### (c) An interaction open across an upward reclassification under-stamps

The capture is frozen at interaction-open (`Interaction.classification`,
`agents/memory/interaction_types.py`) — §C's "classification is read once
per interaction, not re-derived per episode or per fact", and the
`session_id` sibling-mislabel precedent. The close-path stamp sites
(`close_path.py`, `fact_extractor.py`) apply that one captured value to
every row the interaction produces.

If an operator raises a channel's classification while an interaction is
open, the post-raise turns are consolidated into an episode (and a facts
batch) stamped at the **pre-raise** level. Interaction closure is driven by
the idle gap (600s default), the turn cap, or a structural/vote/rotation
close, so the window is not narrow on a quiet group channel.

The RFC's [Non-Goals](../rfcs/0037-memory-confidentiality-channel-classification.md)
entry — *"Retroactive reclassification… re-deriving historical protection
levels after a later reclassification is out of scope"* — covers rows
written **before** the reclassification. It does not speak to rows written
**after** it from a record captured before it. That is a different case and
is currently neither ruled in nor ruled out.

## Impact

- **(a)** An operator with `secret`-channel history in their notes tier has
  no way to take the RFC's own advice at upgrade time. Once the §D gate is
  live, those notes inject into any `internal` turn until each is rewritten
  under the gate.
- **(b)** Documentation-only today. The cost lands later: whoever implements
  the RFC 0049 v0.4.0 cross-scope pump, or debugs a pre-v0.3.12 row's level,
  starts from a description of a join that never ran.
- **(c)** Under the PR 4 §D gate, an under-stamped episode is injected into
  turns below its true level — the precise disclosure RFC 0037 exists to
  prevent. Bounded by how often operators reclassify a channel mid-
  conversation (rare, and audited from Phase 3), which is why this is filed
  rather than fixed in the dark window.

## Proposed fix / investigation path

- **(a)** PR 4 owns the notes leg — implement the flag there (an env var or
  a `migrate` subcommand argument consumed by the v16 handler's notes arm),
  or amend §C to withdraw the promise and say plainly that the only remedy
  is rewrite-under-gate.
- **(b)** PR 8 (closeout) is the docs PR: amend §C's *Migration backfill*
  paragraph to state the blanket `internal` default and the two reasons it
  is equivalent (cross-store unreachability + the v11 channel backfill).
- **(c)** Decide, then write it down. Two candidate rules:
  1. *Accept and document* — add a §C sentence making the live case
     explicit, with the audit trail (Phase 3) as the detection path.
  2. *Close the interaction on reclassification* — the orchestrator already
     mints the reclassification event; a close-on-reclassify propagation
     would split the interaction at the boundary, exactly like the RFC 0030
     wire-id rotation seam (`interaction_boundary.py`), so the pre- and
     post-raise halves stamp at their own levels. More machinery, but it is
     the only option that makes the stamp truthful.

## Notes

> 2026-07-26 — initial capture during the PR #775 (RFC 0037 PR 3) review.
> The same review's two fixed findings (`agent_id` on `memory_projections`,
> the untested wire→capture→stamp seam) landed in that PR.

> 2026-07-26 — **(a) RESOLVED by RFC 0037 PR 4** (the §D hard-gate PR): the
> one-time flag ships as the `PERSATRIX_NOTES_BACKFILL_PROTECTION_LEVEL`
> env var, consumed by the v16 handler's notes arm exactly as proposed —
> honoured only at the moment the notes `protection_level` column is
> created (every row present then is a pre-migration note), notes-scoped,
> validated against the §A vocabulary (an invalid value fails the
> migration loudly rather than silently defaulting), and inert with a
> WARNING once v16 has applied. Pinned by
> `tests/unit/python/test_protection_migration.py::TestNotesBackfillFlag`;
> the vocabulary spelling is drift-pinned to `CLASSIFICATION_RANKS` in
> `test_protection_stamping.py`. (b) remains PR 8's docs leg; (c) remains
> an open §C decision.
