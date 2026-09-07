---
id: ISSUE-0132
summary: "The RFC 0037 §D memory-injection egress gate decides admissibility from the acting CHANNEL's classification alone (`agents/persona_runtime/injection_gate.py` ranks each entry's `protection_level` against the acting level); who is actually in the room at injection time is not an input. Since RFC 0049 Phase 1 made facts cross-room by default and the §A stamping default is `internal` for both a DM and an ordinary group channel, a fact the persona learned from Alice in a DM is admissible in any equally-classified room — including one where Bob is present. The control that exists is coarser than it looks: there is no per-DM classification lever, only the fleet-wide creation-time `dm_default_classification` — raising it raises EVERY DM the fleet opens, an already-open DM keeps its creation-time stamp, and `SetChannelClassification` reclassifies config-declared GROUP channels only (its second caller, the audited reclassification surface, is unbuilt). There is no per-person dimension, so the persona cannot distinguish 'this room' from 'this room WITH BOB IN IT', and the natural human→persona→human expectation — the persona does not repeat what I told it in front of someone I did not tell — has no mechanism behind it."
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

The control that does exist is coarser than "classify Alice's DM". There
is **no per-DM classification lever at all**. DMs open on demand as
`dm:<a>:<b>` with no per-channel config block, so their only declaration
point is `dm_default_classification` (`schemas/channel.schema.json`) —
which is **fleet-wide** and stamped **at creation**. Raising it raises
every DM in the fleet, and only for DMs opened afterwards: an existing
DM keeps its creation-time stamp. `SetChannelClassification`
(`internal/channels/store.go`) has, in its own words, "two callers by
design" — the startup reconcile's adoption step for config-declared
**group** channels (`router_reconcile.go`), and "the future audited
reclassification surface", which is not built. So an already-open DM
with Alice cannot be reclassified through any shipped surface.

What no control here can express is an audience — the same room is more
or less safe depending on who is in it, and the ledger that could answer
this already exists (RFC 0035 `membership_intervals`).

## Impact

- **The human→persona→human case has no boundary.** Two people share a
  persona; A discloses something in a DM; the persona volunteers it in a
  room A never invited B into. Nothing in the gate objects.
- **There is no workaround at DM granularity, and the one that exists
  costs more than the feature.** For a DM already open with Alice,
  nothing shipped can reclassify it. For future DMs the only lever is
  the fleet-wide `dm_default_classification`, which raises *every* DM
  the fleet opens and withholds their content from every lower room —
  including the many rooms where it would have been welcome.
  Confidentiality and usefulness therefore trade off at **fleet**
  granularity when the real distinction is per-person.
- **It compounds with ISSUE-0131.** Without a speaker axis the persona
  cannot even ask "did the person in front of me tell me this?", so the
  audience check has nothing to check against.
- **v0.4.0 raises the stakes.** RFC 0012 clearance is an authority
  dimension over the same egress decision; adding audience afterwards
  means re-opening a gate that org logic already depends on.

## Proposed fix / investigation path

An RFC 0037 amendment adding audience as an additional AND-condition on
the existing §D decision, not a new lattice: resolve the acting channel's
membership, and withhold an entry whose provenance is a room/person
disjoint from that audience unless the entry is marked shareable.

Membership resolution should reuse what the turn already does, not open
a second path. `agents/persona_runtime/channel_roster.py`
(`HttpChannelRosterFetcher`) already fetches channel membership from
`GET /api/v1/channels/{id}` and is wired into the *same* injection path
the §D gate runs on — `memory_context.py` calls `inject_channel_roster`
inside the budgeted context build. Building a separate fetch would add a
second per-turn round trip alongside it, the N+1 that module was written
to avoid. (Whether the audience should be that live roster or the
RFC 0035 `membership_intervals` ledger's assertion-time snapshot is the
open question below, and it is a question about *which* membership, not
about how to reach it.) Shadow-first with a measured admit/withhold
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
>
> 2026-09-07 — **v0.3.16 planning-readiness: plan-opening defaults.** The
> amendment's next-steps item 4 names three locks for the plan (default
> posture, assertion-time vs. injection-time audience, whether RFC 0044
> Phase 2 rides); the readiness audit of the code the fix touches adds the
> rest. Each is a default the plan locks and the first ISSUE-0132 PR may
> overturn at review — recorded here so the plan opens with no dangling
> question, not decided here.
>
> - **Default posture → shadow-first for the whole cycle; the flip is gated
>   on the measured delta, never scheduled.** The shadow counts two withhold
>   causes separately — *disjoint* (the acting room holds a member the
>   entry's source room did not) and *unknown* (membership unresolvable) —
>   so the verdict can tell the gate working from the gate blind. The flip
>   default, when the verdict allows one, is **withhold-disjoint /
>   admit-unknown**: withhold-unknown is the safe reading but degrades a
>   persona that has been useful for four releases silently, and the
>   amendment's risk row makes the measurement the guard, not the default.
>   Entries at `public` are always shareable — the lattice already defines
>   `public` as "no confidentiality expectation" — so audience applies to
>   `internal` and above, and no new "shareable" marking lands this cycle.
> - **Which membership → injection-time, for both rooms, from the roster
>   fetch the turn already makes.** The entry's audience is the *current*
>   member set of its `source_channel_id` (episodes and facts carry it since
>   RFC 0037 §C; notes do not and stay out of the shadow); the acting
>   audience is the current member set of the acting channel. Distinct
>   source rooms among a turn's candidates are fetched once each, cached per
>   turn. The RFC 0035 assertion-time snapshot is the refinement, taken only
>   if the verdict shows churned rooms matter: the only ledger endpoint
>   (`GET …/members/{participant_id}/history`) is per participant and
>   authenticated, so an assertion-time answer needs a new endpoint or an
>   audience column stamped at §C time — both wider than a shadow PR.
> - **Audience is the type-agnostic member-id set.** A persona in the acting
>   room that was not in the source room counts as audience — an agent→agent
>   leak is a leak. This keeps the agent directory out of the decision,
>   which matters for the next point.
> - **The roster fetcher needs two changes before it can feed the gate; both
>   belong to the first ISSUE-0132 PR.** `HttpChannelRosterFetcher.fetch`
>   returns `None` when the agents-directory call fails, and that call fails
>   with `401` for the whole fleet under `auth.mode: enabled`
>   ([ISSUE-0140](ISSUE-0140-agent-fleet-401-on-roster-fetch-under-auth.md)),
>   so today the *public* channel-members half is discarded with it. And
>   `inject_channel_roster` runs for `group:` events only and *after* the §D
>   gate in `_inject_memory_context`, while the audience check must run for
>   every channel-anchored turn (a DM with Bob is an audience) and *before*
>   the gate. The fetch splits so the members half survives a directory
>   miss, and it moves ahead of the gate. With the id-set default,
>   ISSUE-0140 is **not** on this issue's path; it returns only if the
>   verdict motivates a person-only audience.
> - **Composition with §E → an audience withhold is terminal.** A projection
>   lowers an entry's *classification*, not its audience, so the projection
>   branch does not substitute for an audience-withheld entry; it is withheld
>   entirely, and the §G tripwire watch sees it as withheld.
> - **The RFC 0037 amendment is a separate file** (the RFC 0049 L1
>   precedent): the RFC sits at 7 973 of 8 000 words.
> - **Where the wiring lands is at its cap.** `memory_context.py` is at
>   500/500 lines, so the gate wiring starts with a structural split —
>   [ISSUE-0143](ISSUE-0143-debt-sweep-26-files-at-size-cap.md) Workstream
>   D is on this issue's path, not only ISSUE-0137's.
> - **Live leg → a new leg on
>   [MT-PERSONA-CONFIDENTIALITY-001](../manual-tests/MT-PERSONA-CONFIDENTIALITY-001.md)**,
>   authored by the shadow PR before the paid arc, not at closeout.
>   Vacuity rule: under `auth.mode: enabled` the v0.3.15 tenant partition
>   already withholds Alice's DM content from any agent-origin turn, so a leg
>   where a persona volunteers it in a room passes for the wrong reason. The
>   leg has **Alice herself** ask in a room where Bob is present — her
>   tenant, her entry admissible on both the classification and the
>   principal axes, audience the only thing that can withhold it — and runs
>   once more with `auth.mode: disabled`, where everything is `local` and
>   audience is the only boundary at all. MT-MEMORY-GROUP-TENANT-001 stays
>   untouched: its Leg 5 row is this topology but closes on the tenant axis,
>   and the file has 14 words of headroom.
