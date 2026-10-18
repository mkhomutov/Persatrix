---
id: ISSUE-0186
summary: "A persona's memory clean-up still treats everyone it talks to as one pool: episode expiry, the episode size cap, procedural decay and the superseded-fact prune are counted and applied per agent, not per person (principal). One busy person can push another person's episodes out of the cap. v0.3.14 named this a Known Gap for v0.4.0 and v0.3.15 carried it, but no issue held it and v0.3.16's Known Gaps dropped it."
status: open
severity: low
area: memory
created: 2026-10-18
refs:
  - agents/memory/eviction.py
  - agents/memory/facts.py
  - agents/memory/notes.py
  - docs/v0.3.14-plan.md
---

# ISSUE-0186: Memory clean-up is shared across the people a persona talks to

## Context

Since v0.3.14 a persona talking to several signed-in people keeps their
memories apart when it reads: recall filters by the person (principal).
The clean-up that keeps memory within bounds does not. The
[v0.3.14 plan](../v0.3.14-plan.md) split the leftovers by kind. Erasure was
fixed in that release. The clean-up passes were left per agent as **capacity
policy, not read-confidentiality**, and named a Known Gap "scoped to v0.4.0",
with "a v0.4.0 quota design" to own it. The v0.3.14 and v0.3.15 release
checklists listed it; the v0.3.16 checklist did not, and no issue ever held
it.

Read at `16183a01`:

- `_evict_ttl` and `_evict_size_cap` in
  [`agents/memory/eviction.py`](../../agents/memory/eviction.py) select and
  delete episodes by `agent_id` only. The size cap ranks every person's
  episodes together and removes the lowest-scoring excess.
- `FactStore.prune` in [`agents/memory/facts.py`](../../agents/memory/facts.py)
  deletes superseded facts by `agent_id` only.
- Procedural decay runs in the same eviction pass, per agent.
- Note pruning is already narrower: [`agents/memory/notes.py`](../../agents/memory/notes.py)
  prunes per agent, session, principal and epoch.

## Impact

With `auth.mode: enabled` and several people talking to one persona, the
person who talks most fills the episode cap, and the size-cap pass then
removes other people's lower-scoring episodes. Nobody reads anyone else's
memory: recall still filters by principal. But a quiet person's history can
be evicted by someone else's volume. Single-user and `disabled` deployments
are unaffected.

## Proposed fix / investigation path

A quota design, as v0.3.14 said: decide whether caps are per principal, per
agent with a per-principal floor, or per agent as now with the rule stated.
Then partition the size-cap and TTL passes to match. The superseded-fact
prune only removes rows already replaced, so it can likely stay per agent.

## Slot

Not slotted. [v0.4.0's shape](../v0.4.x-sequencing.md#amendment-2026-10-18--v040s-shape-its-prerequisites-its-cuts-and-the-end-of-the-v03x-line)
does not take it in: its [ruling (q)](../v0.4.x-sequencing.md#rulings) boards
an issue only when a phase in the release is its fix. Per-person memory
bounds are also next to the memory isolation work that ruling (b) holds back
without an outside ask.

## Notes

> 2026-10-18 — filed at the v0.4.0 planning-readiness audit. A prior cycle
> named this obligation and the record of it was dropped, as with the
> ISSUE-0146 re-file at the v0.3.16 audit. The functions above were read at
> `16183a01`; no code changes.
