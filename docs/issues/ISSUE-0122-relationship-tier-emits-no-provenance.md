---
id: ISSUE-0122
summary: "The `relationship` tier is the only memory tier that charges the RFC 0017 budget without recording an admission: `render_relationship_section` calls `budget.try_add(...)` but never `budget.record_admission(...)`, while `episodic`/`notes`/`facts`/`channel_history` all pair the two. `relationship` IS in `KNOWN_TIERS` (reserved so future wiring lands on a known name), so `PERSATRIX_MEMORY_PROVENANCE=1` emits no `persatrix.memory.tier_admitted` line for the cross-room person-identity read (RFC 0031 F-7) even when the identity line is injected and paid for. Operator consequence: the one tier answering \"does the persona know who I am?\" is invisible to the provenance switch, so zero admissions on an identity turn is indistinguishable from a recall miss. Found live at the v0.3.13 release-prep arc, where it had already produced a wrong diagnosis note in MT-MEMORY-CROSSROOM-001 (execution report F-3)."
status: open
severity: low
area: agents
created: 2026-08-05
refs:
  - agents/persona_runtime/relationship_section.py
  - agents/persona_runtime/memory_budget.py
  - docs/rfcs/0031-amendment-cross-room-person-identity.md
  - docs/manual-tests/MT-MEMORY-CROSSROOM-001.md
  - docs/manual-tests/v0.3.13-execution-report.md
---

## Summary

Every channel-derived memory tier reports what it admitted, except one. Under
`PERSATRIX_MEMORY_PROVENANCE=1` the `MemoryBudget` emits a
`persatrix.memory.tier_admitted` record per admitted entry — the operator's only
window into what actually reached a turn's prompt. The `relationship` tier,
which carries the RFC 0031 F-7 cross-room person identity, never emits one.

It is not that the tier is unknown to the machinery. `relationship` is a member
of `KNOWN_TIERS` in [`memory_budget.py`](../../agents/persona_runtime/memory_budget.py),
reserved with an explicit comment that future wiring should "land on a known
name rather than coining a new one in a follow-up PR". The admission call is
simply absent at the call site.

## Evidence

`render_relationship_section` charges the budget and stops there:

| Tier module | `try_add` calls | `record_admission` calls |
|---|---|---|
| `episodic_section.py` | 2 | 2 |
| `notes_section.py` | 1 | 2 |
| `channel_history.py` | 3 | 1 |
| `facts_section.py` | 9 | 2 |
| **`relationship_section.py`** | **1** | **0** |

The identity text wins budget space through `budget.try_add(rel_text,
min_tokens=MIN_TOKENS_RELATIONSHIP)` and is injected — it is paid for and
delivered — but nothing records that it was.

## Why it matters

The gap is not merely cosmetic; it has already caused a wrong diagnosis to be
written down. `MT-MEMORY-CROSSROOM-001`'s Leg 2b diagnosis note used to tell
the operator to read the injected identity line out of the provenance stream.
Because no such line exists, an executor following that instruction on a
*correctly working* identity recall would see zero admissions and conclude the
recall had failed. That note was corrected at the v0.3.13 release-prep arc
([execution report F-3](../manual-tests/v0.3.13-execution-report.md#findings--follow-ups)),
but the correction documents the blind spot rather than removing it.

The affected question is the one an operator most wants provenance for: *does
this persona actually know who it is talking to, or is it reading the name off
the room transcript?* That is precisely the confound Leg 2b had to be re-run on
an empty-transcript channel to eliminate — and provenance is what would have
settled it directly.

## Proposed fix

Pair the existing `try_add` with a `record_admission(tier="relationship", ...)`
at the admission site in `relationship_section.py`, matching the shape the four
other tiers already use. No new tier name is needed (`KNOWN_TIERS` already
carries it), no schema change, and the emission stays behind the existing
`PERSATRIX_MEMORY_PROVENANCE` switch, so default behaviour is unchanged.

Care is warranted on **what** the record carries: the identity object holds a
person's name and role, and the provenance log is its own egress surface. The
record should follow the existing tier convention — ids, tier, and the
protection level — and must not embed the identity text itself.

## Scope note

Filed from the v0.3.13 release-prep live arc as a carry-forward, not a
regression: the behaviour predates the release and no v0.3.13 change touched
it. [ISSUE-0121](ISSUE-0121-crossroom-person-identity-legs-never-run-live.md)
closed on the recorded legs 1b/2b results; this is the observability residual
that running them surfaced.
