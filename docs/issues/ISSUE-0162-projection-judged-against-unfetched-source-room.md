---
id: ISSUE-0162
summary: "A persona can pass the gist of a private-room memory on to a room that adds someone new. The v0.3.16 audience check judges a §E projection against the original episode's source room, but the turn fetches only the rooms of entries §D admits, so that room reads fetch-failed and live admits the projection. It happens whenever no other admitted entry of the turn comes from that room. The v0.3.16 documents blamed a missing acting member set."
status: open
severity: medium
area: memory
created: 2026-10-21
refs:
  - docs/rfcs/0037-amendment-audience-egress.md
  - docs/v0.3.16-scope-locks.md
  - docs/v0.3.x-sequencing.md
  - docs/v0.4.x-sequencing.md
  - docs/v0.4.0-plan.md
  - docs/v0.3.16-release-checklist.md
  - docs/manual-tests/v0.3.16-execution-report.md
  - docs/issues/ISSUE-0132-memory-egress-gate-blind-to-room-audience.md
  - agents/persona_runtime/audience.py
  - agents/persona_runtime/injection_gate.py
  - agents/persona_runtime/projection_branch.py
  - tests/unit/python/test_audience_composition.py
  - tests/unit/python/test_audience_scope.py
---

# ISSUE-0162: A §E projection is judged against a room the turn never fetched, so `live` serves it in any room

## Summary

v0.3.16 withholds a memory from a room that adds someone the memory's
source room did not hold ([ISSUE-0132](ISSUE-0132-memory-egress-gate-blind-to-room-audience.md)).
One path gets past that check. When the §D gate withholds an episode
because its level is above the room's, §E serves a lower-level
**projection** of it instead. The projection is supposed to face the
audience check against the *original* episode's source room
([`TurnInjectionGate.audience_admits_projection`](../../agents/persona_runtime/injection_gate.py),
called from `rebuild` in
[`projection_branch.py`](../../agents/persona_runtime/projection_branch.py)).
It never can, because the turn never fetches that room's members.

`resolve_turn_audience` fetches only the source rooms of entries §D will
admit: `_distinct_source_rooms` in
[`audience.py`](../../agents/persona_runtime/audience.py) skips every entry
ranked above the acting level, and every entry whose label rule (c) cannot
read. Those are exactly the entries §E stands in for. So the room is
missing from `TurnAudience.rooms`, `TurnAudience.verdict` answers
`withhold-unknown-fetch-failed`, and `live` admits that verdict, because
`ENFORCED_VERDICTS` holds `withhold-disjoint` only. A projection of an
episode from another room therefore reaches the prompt with no
membership comparison, unless another entry that §D admits in the same
turn comes from the same source room, which fetches it. The acting
room's roster makes no difference: it resolves, and the verdict is
*fetch-failed* all the same.

## Context

**Seen live, misdiagnosed.** On the v0.3.16 release arc (run C, Leg 4 of
[MT-PERSONA-CONFIDENTIALITY-001](../manual-tests/MT-PERSONA-CONFIDENTIALITY-001.md)),
the projection of the `restricted` war-room episode was served in
`planning` and recorded `withhold-unknown-fetch-failed` with
`fetches 0, source_rooms 0`
([execution report, evidence obligation 2](../manual-tests/v0.3.16-execution-report.md#evidence-obligation-2--the-fetch-failed-count-both-runs)).
The war room holds two members, `ember-owl` and `alex`; `planning` holds
more, so the check as designed would have said *disjoint*. The release
recorded this as a known gap, but blamed a missing **acting** member set.
The acting member set is there; the **source** room is what nobody
fetched. The wrong cause was in the
[CHANGELOG](../../CHANGELOG.md) `[0.3.16]` entry for ISSUE-0132 (its
**Limits** clause), the
[release checklist §6](../v0.3.16-release-checklist.md#6-known-gaps-to-document-in-release-notes)
and the execution report. Each now states the cause above.

**Reproduced at `main` `435acc6a`**, through the real resolution:

- acting room `group:standup` with members {alice, bob, ember-owl}, from a
  resolved `ChannelRoster`;
- a roster fetcher that would answer `dm:alice:ember-owl` → {alice,
  ember-owl};
- a `restricted` episode from that DM, with a stored `internal`
  projection, recalled on an `internal` turn in the standup.

The turn made **0** roster calls (`fetches 0, source_rooms 0`), the
projection's verdict was `withhold-unknown-fetch-failed`,
`audience_admits_projection(...)` returned `True`, and the projection was
served. The standup adds Bob, so the verdict should be
`withhold-disjoint`, with nothing served. The same happened in both
episode tiers (`episodic` and `channel_history`), for an episode whose
level is above the room's and for one whose label rule (c) cannot read.
Add a second episode from the same DM that §D admits and the picture
changes: the DM is fetched once, the projection reads *disjoint* and
nothing is served. So the gap needs a turn in which no admitted entry
comes from the projection's source room.

**Why the tests missed it.** `test_audience_composition.py` builds
`TurnAudience` by hand with the DM's members already in `rooms`, so it
never runs the fetch set. `test_audience_scope.py` pins that an
above-rank entry costs no fetch — right for a fact, and wrong for an
episode §E can stand in for.

## Impact

- **The audience condition does not hold for projections.** Under
  `live`, the shipped default, an episode withheld on classification is
  served as its projection whoever is in the room, on any turn where no
  other entry §D admits comes from the same source room. That inverts
  the release's promise by level: an `internal` fact Alice tells the
  persona in her DM is withheld from the standup with Bob, while a
  `restricted` one reaches it, as its `internal` abstraction. Only the
  two episode tiers are affected; they are the only ones with a
  projection producer, the close of a protected interaction
  ([`finalize_close.py`](../../agents/persona_runtime/finalize_close.py)).
- **The record misreports it.** *Fetch-failed* is meant to be a
  transient cause that clears on the next turn. This one repeats on every
  turn that serves such a projection. It also keeps these
  cases out of the *disjoint* count that the shadow measurement reads.
- **Severity is `medium` on the amendment's reading** — the open question
  below can move it.

## Open question — does a projection disclose anything?

The project's documents disagree, and the severity — and whether the fix
should withhold — follows the answer.

- **Yes.** The [audience amendment §E](../rfcs/0037-amendment-audience-egress.md#e--an-audience-withhold-is-terminal)
  says an abstraction "still tells Bob that Alice raised a rollout
  decision", and the `audience_admits_projection` docstring says the
  same of a projection served in Bob's room. On this reading the gap is
  the ISSUE-0132 leak at the projection's level, and the fix below is
  right.
- **No.** The release checklist §6 and the execution report both call a
  projection "non-disclosing by construction" and say nothing leaked. On
  this reading only the recorded verdict is wrong (`low`), and the
  consistent fix is to exempt projections from the audience check the
  way `public` entries are exempt, which also removes the reason for the
  amendment's "an audience withhold is terminal" rule.

The maintainer's answer decides which. Until then this issue follows the
amendment, the design document of record.

## Proposed fix / investigation path

Two shapes, both keeping the scope-lock-2 bound of one round trip per
distinct room, deduplicated, with the acting room free:

1. **Fetch eagerly (recommended).** Keep a withheld entry's source room in
   the fetch set when §E could stand in for it: an episode-tier entry
   with an interaction id. One shared rule in `projection_branch.py`
   should decide both what §E looks up and what `_distinct_source_rooms`
   fetches, so the two cannot drift again. The room joins the same
   parallel fetch as every other source room, so the turn waits for no
   extra round trip. The price is a fetch for a withheld episode that
   turns out to have no projection, bounded by the recall limits (5
   episodic and 20 channel-history candidates). The bound then reads:
   one round trip per distinct source room among the entries the gate
   will judge, verbatim or as a §E stand-in.
2. **Fetch lazily** in `apply_episode_projections`, once it knows which
   non-`public` projections it will serve. No wasted fetch, but a second
   round trip after the first on exactly the turns that serve a
   projection, a second place that fetches, and a `TurnAudience` that
   changes after the gate was built on it, so the shadow trace would
   report stale counts.

Shape 1 was tried against `main` at `435acc6a`, in a scratch copy this
change does not keep, with two tests. The first drives the whole path —
`resolve_turn_audience` → `TurnInjectionGate.filter_entries` →
`apply_episode_projections` — for both episode tiers, for an above-rank
level and for an unreadable label, and asserts *disjoint*, nothing served
and one fetch; it fails without the fix. The second pins the widened
fetch set: a withheld episode with an interaction id is fetched, while
one without an interaction id, or a withheld fact, is not. With the fix,
`make eval-replay` still passes all six stable golden traces, so no
recorded prompt moves. Neither the scratch code nor its tests are kept,
since ruling (b) holds the code; the shared fetch rule and these two
tests are the record of what the fix needs.

## Sequencing

The fix does not board v0.4.0. [Ruling (b) of the sequencing Amendment
2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens)
lets no further memory isolation, attribution or audience work board a
release after v0.3.16 unless an amendment's External evidence section
records an outside ask for it, and keeps the §D gate "as built". The
[Amendment 2026-10-18](../v0.3.x-sequencing.md#amendment-2026-10-18--a-society-that-pursues-long-goals-exp-001-measures-v040-opens)
holds (b) as written, and the [v0.4.0 plan's scope
locks](../v0.4.0-plan.md#scope-locks) record no such ask. Ruling (q) of
[v0.4.0's shape](../v0.4.x-sequencing.md#amendment-2026-10-18--v040s-shape-its-prerequisites-its-cuts-and-the-end-of-the-v03x-line)
boards an issue only when a phase that boards is its fix, and no such
phase fixes this one. The plan's
[v0.3.16 follow-up](../v0.4.0-plan.md#v0316-follow-up) asked for this
filing, not for the fix. Whether a correction to v0.3.16's own gate
counts as "further audience work" is the maintainer's call. Until then
this issue records the finding, the fix and its tests, so the next
amendment can weigh them.

## Notes

> 2026-09-18 — found in a code reading of the v0.3.16 audience check and
> reproduced at `main` `8ad2d7e4`.
>
> 2026-10-21 — filed. Re-checked at `main` `435acc6a`: `_distinct_source_rooms`
> is unchanged and the reproduction gives the same result in both episode
> tiers. The misstated cause in the CHANGELOG, the release checklist §6 and
> the execution report is corrected where it is stated; the question of
> whether a projection discloses anything is left to the maintainer, as
> above.
