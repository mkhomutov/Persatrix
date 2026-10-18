---
id: ISSUE-0186
summary: "No memory clean-up runs for a persona, so its episodes and facts grow without limit in every deployment. The eviction loop (episode expiry, the episode size cap, procedural decay) starts only inside `MemoryStore`, which personas never build, and the superseded-fact prune has no caller. Where the loop does run, for task agents, its size cap ranks all of an agent's rows together, which the per-person (principal) quota v0.3.14 deferred to v0.4.0 must decide. That Known Gap was ISSUE-0081's residual; v0.3.16's Known Gaps dropped it."
status: open
severity: low
area: memory
created: 2026-10-18
refs:
  - agents/memory/store.py
  - agents/memory/eviction.py
  - agents/memory/facts.py
  - agents/memory/notes.py
  - agents/persona_runtime/state_persistence.py
  - docs/v0.3.14-plan.md
  - docs/issues/ISSUE-0081-session-id-process-global-not-task-local.md
  - docs/issues/ISSUE-0085-epoch-axis-run-isolation.md
---

# ISSUE-0186: A persona's memory has no capacity bound

## Summary

Nothing removes old memory from a persona. The clean-up passes exist, but
only task agents run them, so a persona's episodes and facts pile up for as
long as it runs. Where the passes do run, one of them, the size cap, ranks
every person's memories together. That is the per-person question v0.3.14
deferred to v0.4.0, and no record has held it since v0.3.16.

## Context

Since v0.3.14 a persona talking to several signed-in people keeps their
memories apart when it reads: recall filters by the person (principal). The
[v0.3.14 plan](../v0.3.14-plan.md) split the leftover per-agent work by kind.
Erasure was fixed in that release. The clean-up passes were left per agent as
**capacity policy, not read-confidentiality**, and named a Known Gap "scoped
to v0.4.0", with "a v0.4.0 quota design" to own it.
[ISSUE-0081](ISSUE-0081-session-id-process-global-not-task-local.md) carried
it as its residual when it closed (its 2026-08-05 note), and the v0.3.14 and
v0.3.15 release checklists listed it. The v0.3.16 checklist did not, and it
never had an issue of its own.
[ISSUE-0085](ISSUE-0085-epoch-axis-run-isolation.md) recorded the same
deferral for the run/test epoch: the passes skip `epoch_id` as they skip
`principal_id`.

Read at `319eb70`:

- The eviction loop runs episode expiry, the episode size cap and procedural
  decay (`EvictionPass.run` in
  [`agents/memory/eviction.py`](../../agents/memory/eviction.py)). Only
  `MemoryStore.initialize` in
  [`agents/memory/store.py`](../../agents/memory/store.py) starts it.
  `BaseAgent.initialize_memory` builds a `MemoryStore` for task agents, and
  `agents/server.py` skips personas there. A persona opens its tiers itself
  ([`state_persistence.py`](../../agents/persona_runtime/state_persistence.py))
  and never builds one. A comment in `agents/memory/_migration_registry.py`
  already says a persona's episode count "has no ceiling".
- `FactStore.prune` in [`agents/memory/facts.py`](../../agents/memory/facts.py),
  the superseded-fact prune, has no caller outside tests. Neither do
  `EpisodicMemory.summarize_old_episodes` and `delete_old_episodes`.
- Of the passes, only the size cap compares one row with another: it ranks
  all of an agent's episodes and removes the lowest-scoring excess. Expiry
  and decay judge each row on its own values, and so does the prune.
- A task agent's memory takes its principal and epoch from the process.
  `ExecuteTask` binds neither per request, so today the size cap only ever
  sees one person's rows.
- Note pruning is the one bound a persona has, and it is already per person:
  [`agents/memory/notes.py`](../../agents/memory/notes.py) prunes on each
  write per agent, session, principal and epoch.

## Impact

A persona's episode and fact tables grow without limit, in every deployment
and auth mode, single-user included. Recall stays correct. What grows is disk
use and the cost of anything that reads a persona's whole history. A society
meant to run for weeks is where this shows.

The per-person question is real but latent. Once a persona's memory has a
size cap, or one task agent holds several people's rows, the person who talks
most fills the cap and the pass removes other people's lower-scoring
episodes. Nobody reads anyone else's memory either way.

## Proposed fix / investigation path

1. **The quota design v0.3.14 asked for**, decided before any cap reaches a
   persona: caps per principal (and per epoch), per agent with a
   per-principal floor, or per agent as now with the rule stated. Only the
   size cap needs partitioning to match. Expiry, decay and the prune judge
   each row alone, so partitioning them changes nothing.
2. **A bound for persona memory**: run the eviction pass, or a persona-side
   equivalent, over persona tiers, and give the superseded-fact prune a
   caller.

## Slot

Not slotted. [v0.4.0's shape](../v0.4.x-sequencing.md#amendment-2026-10-18--v040s-shape-its-prerequisites-its-cuts-and-the-end-of-the-v03x-line)
does not take it in: its [ruling (q)](../v0.4.x-sequencing.md#rulings) boards
an issue only when a phase in the release is its fix. Per-person memory
bounds also sit next to the memory isolation work that ruling (b) holds back
without an outside ask. Like ISSUE-0156 and ISSUE-0157, the missing bound is a
candidate for the plan's goal audit, which may propose it by amendment.

## Notes

> 2026-10-18 — filed at the v0.4.0 planning-readiness audit. A prior cycle
> named this obligation and the record of it was dropped, as with the
> ISSUE-0146 re-file at the v0.3.16 audit. The obligation was a named Known
> Gap rather than a promised issue, so the re-file exception is stretched
> slightly. The [auth guide](../guides/auth.md) and
> [MT-MEMORY-MULTIUSER-001](../manual-tests/MT-MEMORY-MULTIUSER-001.md) now
> point here instead of at ISSUE-0082 and a v0.4.0 slot. The code above was
> read at `319eb70`; no code changes.
