---
id: ISSUE-0132
summary: "The RFC 0037 §D memory-injection egress gate decides admissibility from the acting CHANNEL's classification alone (`agents/persona_runtime/injection_gate.py` ranks each entry's `protection_level` against the acting level); who is actually in the room at injection time is not an input. Since RFC 0049 Phase 1 made facts cross-room by default and the §A stamping default is `internal` for both a DM and an ordinary group channel, a fact the persona learned from Alice in a DM is admissible in any equally-classified room — including one where Bob is present. The control that exists is real but coarse and manual: an operator can classify Alice's DM `restricted`/`secret`, which withholds it everywhere lower. There is no per-person dimension, so the persona cannot distinguish 'this room' from 'this room WITH BOB IN IT', and the natural human→persona→human expectation — the persona does not repeat what I told it in front of someone I did not tell — has no mechanism behind it."
status: open
severity: medium
area: memory
created: 2026-08-19
refs:
  - docs/rfcs/0037-memory-confidentiality-channel-classification.md
  - docs/rfcs/0049-memory-consolidation-gradient.md
  - docs/rfcs/0035-channel-membership-interval-ledger.md
  - agents/persona_runtime/injection_gate.py
  - agents/persona_runtime/classification.py
  - agents/memory/fact_types.py
---

## Summary

The memory egress gate knows what kind of room it is speaking into. It
does not know who is listening.

## Context

`TurnInjectionGate` (`agents/persona_runtime/injection_gate.py`) resolves
an acting classification for the turn — off the wire stamp for a
channel-anchored event, the rule-(b) `public` floor otherwise — and
withholds any entry whose `protection_level` outranks it. The inputs are
the entry's label and the channel's label. Channel MEMBERSHIP is not
consulted, at injection time or anywhere else in the gate.

The §A lattice is `public < internal < restricted < secret` with a
stamping default of `internal` (`classification.py`). A persona DM and an
ordinary project channel therefore sit at the same level by default, and
since RFC 0049 Phase 1 facts are cross-room by design — that is the
shipped v0.3.12 headline, "a project fact taught in a DM is known in the
standup".

The coarse control is available and works: classify the DM `restricted`
and its content is withheld from every `internal` room. What it cannot
express is an audience — the same room is more or less safe depending on
who is in it, and the ledger that could answer this already exists
(RFC 0035 `membership_intervals`).

## Impact

- **The human→persona→human case has no boundary.** Two people share a
  persona; A discloses something in a DM; the persona volunteers it in a
  room A never invited B into. Nothing in the gate objects.
- **The workaround costs the feature.** The only way to prevent it today
  is to raise the DM's classification, which withholds the content from
  every lower room — including rooms where it would have been welcome.
  Confidentiality and usefulness trade off at channel granularity when
  the real distinction is per-person.
- **It compounds with ISSUE-0131.** Without a speaker axis the persona
  cannot even ask "did the person in front of me tell me this?", so the
  audience check has nothing to check against.
- **v0.4.0 raises the stakes.** RFC 0012 clearance is an authority
  dimension over the same egress decision; adding audience afterwards
  means re-opening a gate that org logic already depends on.

## Proposed fix / investigation path

An RFC 0037 amendment adding audience as an additional AND-condition on
the existing §D decision, not a new lattice: resolve the acting channel's
current membership (RFC 0035 interval ledger), and withhold an entry
whose provenance is a room/person disjoint from that audience unless the
entry is marked shareable. Shadow-first with a measured admit/withhold
delta, the pattern RFC 0049 Phase 1 already used, so the quality cost of
the tighter gate is observed before it is enforced.

Open questions worth naming before any PR: what the default posture is
(withhold-unknown is safe but silently degrades a persona that has been
useful for four releases), whether a fact's audience is derived from its
source room's membership at ASSERTION time or at injection time, and how
this composes with the §E declassification-projection branch.

## Notes

> 2026-08-19 — filed while considering pre-v0.4.0 scope across the
> conversational topologies. Slotted **v0.3.16** by the
> [sequencing Amendment 2026-08-19](../v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train)
> — after ISSUE-0131, which supplies the attribution this gate needs to
> reason about, and before the v0.4.0 clearance work that would otherwise
> build on top of an audience-blind gate.
