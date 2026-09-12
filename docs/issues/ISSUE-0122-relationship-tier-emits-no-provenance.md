---
id: ISSUE-0122
summary: "The `relationship` tier is the only memory tier that charges the RFC 0017 budget without recording an admission: `render_relationship_section` calls `budget.try_add(...)` but never `budget.record_admission(...)`, while `episodic`/`notes`/`facts`/`channel_history` all pair the two. `relationship` IS in `KNOWN_TIERS` (reserved so future wiring lands on a known name), so `PERSATRIX_MEMORY_PROVENANCE=1` emits no `persatrix.memory.tier_admitted` line for the cross-room person-identity read (RFC 0031 F-7) even when the identity line is injected and paid for. Operator consequence: the one tier answering \"does the persona know who I am?\" is invisible to the provenance switch, so zero admissions on an identity turn is indistinguishable from a recall miss. Found live at the v0.3.13 release-prep arc, where it had already produced a wrong diagnosis note in MT-MEMORY-CROSSROOM-001 (execution report F-3)."
status: resolved
severity: low
area: agents
created: 2026-08-05
closed: 2026-09-12
closed_pr: 941
refs:
  - tests/unit/python/test_relationship_admission.py
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

## Notes

> 2026-08-19 — **slotted v0.3.16** by the [sequencing Amendment 2026-08-19](../v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train), paired with
> [ISSUE-0108](ISSUE-0108-reasoning-reason-note-no-operator-egress.md) as the
> operator-observability half of that release: the v0.3.16 audience gate
> ([ISSUE-0132](ISSUE-0132-memory-egress-gate-blind-to-room-audience.md)) adds
> a new withhold reason, and an operator cannot read why the persona decided
> what it decided while the identity tier still charges the RFC 0017 budget
> without recording an admission.
>
> 2026-09-07 — **v0.3.16 planning-readiness: plan-opening default.** One PR,
> no lock needed: pair the `try_add` with
> `record_admission(tier="relationship", …)`. Its signature is pinned to
> `(tier, item_id, tokens_admitted)` and the record shape is deliberately
> narrow, so carrying the protection level too is the one signature
> widening the PR has to argue at review — never the identity text, the
> provenance log being an egress surface of its own. The same PR flips the
> [MT-MEMORY-CROSSROOM-001 Leg 2b](../manual-tests/MT-MEMORY-CROSSROOM-001.md)
> "provenance does not see this leg" note, since the line it says never
> exists will exist. The `relationship` tier stays outside the RFC 0037 §D
> gate (the RFC's Non-Goals): this adds an admission record, not a gate
> decision. Rides with ISSUE-0108 as the one observability workstream.
>
> 2026-09-08 — **Locked at the v0.3.16 plan opening** ([v0.3.16 plan](../v0.3.16-plan.md) PR B1). Also carries ISSUE-0137's one-sentence statement that the relationship tier stays ambient-only on the tenant axis, since B1 touches that tier anyway.
>
> 2026-09-12 — **Resolved by v0.3.16 PR B1** ([#941](https://github.com/mkhomutov/Persatrix/pull/941)).
> `render_relationship_section` now pairs its `try_add` with
> `record_admission(tier="relationship", …)`, charged with the tokens the
> budget actually took (measured off `budget.remaining` around the call,
> so the oversized-truncation path charges what it admitted). The record
> is keyed by the pair the relationship row is keyed by —
> `<other_participant_type>:<other_participant_id>`, e.g. `user:alex` —
> which is what lets an operator read a wrong participant type (the
> ISSUE-0119 class) straight off the `tier_admitted` line. **The
> signature is not widened**: the relationship tier sits outside the RFC
> 0037 §D gate and its entries carry no `protection_level`, so there is
> nothing for a third field to carry, and the identity text never lands
> on the record (pinned by a test that flattens every string attribute of
> the emitted `LogRecord` and asserts the name, role, preferences and raw
> tail are absent). The pin is
> `tests/unit/python/test_relationship_admission.py`: the registry pairing
> at the render helper, the structured record under
> `PERSATRIX_MEMORY_PROVENANCE=1`, silence without it, and an identity turn
> driven through `create_persona_agent` in a second room that emits exactly
> one `relationship` admission — so zero admissions on such a turn is now a
> recall or wiring miss, no longer the expected reading.
> MT-MEMORY-CROSSROOM-001's Leg 2b note is flipped in the same PR; the
> relationship tier is stated ambient-only on the tenant axis in
> `relationship_section.py`'s module docstring (ISSUE-0137's sentence);
> `KNOWN_TIERS`' "tiers that do not currently call `record_admission`"
> comment is retired, since none is left. Default behaviour unchanged: the
> emission stays behind the switch; the in-memory registry fills either way
> and nothing reads the `relationship` bucket yet.
