---
id: ISSUE-0165
summary: "A persona treated a peer as a stranger in every session except the one that first wrote the peer's relationship row. The row has no session in its primary key, and relationships are meant to follow a peer across channels (Decision 3 of docs/memory-scope-axes.md), yet `get_trust`, `get_relationship_summary` and `get_all_relationships` filtered the row on the session it was first written in (RFC 0031 Phase 2 PR 3). Once the orchestrator gave each persona one session per channel (#459), two cases broke. With `PERSATRIX_SESSION_ID` set at start-up, every peer seeded from `relationships:` config carries that start-up session, so the configured trust never reached a channel's prompt. And a peer first met under one session was hidden in every later session, along with that session's own interactions with them. The fix reads the row without the session filter, as `get_identity` already did, and keeps the interaction history per session."
status: resolved
severity: medium
area: memory
created: 2026-09-22
closed: 2026-09-22
closed_pr: 980
refs:
  - agents/memory/relationship.py
  - agents/memory/relationship_queries.py
  - agents/memory/relationship_mutations.py
  - agents/persona_runtime/state_persistence.py
  - agents/persona_runtime/relationship_section.py
  - agents/persona_runtime/record_close.py
  - internal/channels/session_binding.go
  - internal/channels/grpc_dispatcher.go
  - cli/src/session_resolve.rs
  - tests/unit/python/test_relationship_row_cross_session.py
  - tests/unit/python/test_relationship_session_scope.py
  - tests/integration/test_prompt_path_sessions.py
  - docs/memory-scope-axes.md
  - docs/rfcs/0031-per-session-namespacing-channels.md
  - docs/rfcs/0031-pr-plan.md
  - docs/rfcs/0031-phase2-pr-plan.md
  - docs/issues/ISSUE-0080-relationship-recent-interactions-cross-session-leak.md
  - docs/experiments/EXP-001-preregistration.md
---

# ISSUE-0165: A relationship is hidden outside the session that first wrote it

## Summary

A persona keeps one relationship row per peer: the trust score, a note on
the last trust change, and the peer's identity. The row records the session
it was first written in, and until this fix every read of trust filtered on
that record. Each channel runs under its own session, so a peer read as a
stranger everywhere except where the row began. A peer given a trust level in
`config/agents.yaml` read as a stranger in every channel whenever
`PERSATRIX_SESSION_ID` was set.

## Context

**Where the tag comes from.** The `relationships` row is one per agent, peer,
tenant (principal) and epoch; the session is not part of its primary key. The
[RFC 0031 §C amendment](../rfcs/0031-per-session-namespacing-channels.md#c-storage-model)
calls it "cross-session shared, first-seen-tagged", with the per-session
views drawn from `interactions`. `record_interaction` stamps the row's
`session_id` when it inserts the row and keeps it after that. `seed_trust`
stamps config seeds with the session `initialize_memory` passes: the
persona's start-up session (`PERSATRIX_SESSION_ID`, or `legacy` when unset),
since [finding #2 of the RFC 0031 PR plan](../rfcs/0031-pr-plan.md).

**Where it was read.** RFC 0031 Phase 2 PR 3
([#450](https://github.com/mkhomutov/Persatrix/pull/450), 2026-05-28)
filtered all three reads of the row on `session_id IN (active, legacy)`.
When the row failed the filter, `get_relationship_summary` returned the
"no relationship" summary before it read the interaction history, which has
carried its own per-session filter since
[ISSUE-0080](ISSUE-0080-relationship-recent-interactions-cross-session-leak.md).

**What changed underneath.** Both choices were made while a session meant one
run of the process. The next day,
[#459](https://github.com/mkhomutov/Persatrix/pull/459) (ISSUE-0082 PR 2)
made the orchestrator send a session with every channel message: a UUIDv7
minted the first time it sees an agent and channel together
(`SessionResolver` in `internal/channels/session_binding.go`). The persona
binds it for the turn, and the close path records the turn's interactions
under it. From then on, the session in a channel was never the start-up
session, and it differed from channel to channel.

| Case | Row tagged | Read under | What the persona saw |
|---|---|---|---|
| A peer seeded from config, `PERSATRIX_SESSION_ID=run-boot` | `run-boot` | the channel's UUIDv7 | trust 0.5, no history, no relationship section |
| A peer first met under session S1 | S1 | any other session S2 | the same, S2's own interactions included |

**Who hits it.** The [sessions guide §4](../guides/sessions.md#4-how-the-active-session-is-resolved)
tells operators to set `PERSATRIX_SESSION_ID` when the persona starts, to
scope an arc. MT-MEMORY-005 step 2 pins it for the dementia arc, and
MT-SESSION-001 and -003 start the persona under it. The shipped compose
files do not set it.
Config seeds name agent peers only, and a persona's own publishes carry no
session of their own, so a configured peer always talks under the
orchestrator's session. The CLI forwards `PERSATRIX_SESSION_ID`, or the
pointer `session use` writes, as a per-request override
(`cli/src/session_resolve.rs`). The web console and every persona publish
use the orchestrator's session. So a person who talks to one persona from
both, or who switches the active session, splits one DM across two sessions.

**The documents already said otherwise.** Decision 3 of
[Memory Scope Axes](../memory-scope-axes.md#decisions-taken) makes the
relationship follow the peer across channels. The RFC 0031 §A amendment
confirms it. [ISSUE-0093](ISSUE-0093-person-identity-cross-room-tier.md)
chose this tier to hold identity for that reason. The sessions guide says
trust follows the person, and MT-SESSION-003 says a fresh session does not
reset it. The code disagreed with all of them.

**Why the tests missed it.** `test_session_id_seed_trust.py` checked the
seed's tag, not whether a channel could read the seed.
`test_relationship_session_scope.py` pinned the filter itself as a guard
against one run's state leaking into the next (F-3), a job the epoch axis
has done since ISSUE-0085.

## Impact

- **Configured trust never reached a prompt while the variable was set.**
  In production, trust leaves 0.5 only through config seeds:
  `update_trust` and `apply_decay` have no production caller. With the
  variable set, the shipped seed of ember-owl's trust in iron-fox (0.9)
  read as 0.5 in every channel.
- **A DM with a configured peer never showed its history.** The row read
  failed first, so the relationship section (trust, interaction count, last
  seen, cadence) never rendered, even after closed interactions in that DM.
  A peer not in the config got its section.
- **A split DM lost its history in the second session.** Once one session
  wrote a peer's row, another session saw none of its own interactions with
  that peer.
- **Medium.** Nothing leaked. A configured feature was silently off under a
  setup the documentation recommends, and operators who use session
  overrides lost the history they had built.

Reproduced on `4cbd143c` and on #979's head. With
`PERSATRIX_SESSION_ID=run-boot` and `relationships: [{agent_id: iron-fox,
trust_level: 0.9}]`, `get_trust("iron-fox")` returned 0.9 with no session
bound and 0.5 inside `session_scope(<uuid>)`. After a closed DM interaction
recorded under that session, `get_relationship_summary` reported
`interaction_count` 0, and the persona's prompt had no relationship section.
With the variable unset, the same run read 0.9, counted 1 and rendered the
section. For the second case, a row first written under S1, with two
interactions under S2, read a count of 0 under S2.

## Fix

The row is read with no session filter; the interaction history keeps one.

- `get_trust` reads the row with no session predicate and takes no
  `sessions` argument. `get_relationship_summary` and
  `get_all_relationships` read the row the same way. They apply `sessions`
  only to the interaction history (count, recent interactions, first and
  last seen), as ISSUE-0080's Policy (C) set it. `get_identity` already
  read the row this way.
- Principal and epoch stay strict equality with no carve-out, so the row
  still never crosses tenants or runs.
- The row's `session_id` stays a record of where it began. Seeding is
  unchanged, so MT-SESSION-001 step 7 still holds, and rows already seeded
  under a start-up session are read again with no migration.

**Rejected: seed under `legacy`.** It would fix new seeds only. `INSERT OR
IGNORE` never re-tags a row already seeded under a start-up session, and
ruling (f) rules out a migration. It would also leave the second case open.

**Why no gate is needed.** The relationship tier stays outside the RFC 0037
§D gate, as its non-goals say. Identity already crossed sessions (F-7
Option D). Trust and the trust note now do too, and in production nothing
said in a channel reaches them: trust comes from config seeds alone, and
`update_trust`, the only writer of the note, has no production caller. The
module docstring of `agents/persona_runtime/relationship_section.py`
records that a production writer of either must take the gate into account
first.

**Tests first.** `tests/unit/python/test_relationship_row_cross_session.py`
adds 8 tests. Against `a8e0b9c8`, the 6 that state the fix failed: seeded
trust inside a channel, the channel's own interactions, the list read, a
later session's own history, one trust value for the pair, and the
relationship section in a real persona prompt. The 2 guards that epoch and
principal stay strict passed before and after. In
`test_relationship_session_scope.py`, the six pins that asserted the old
filter now assert the row is shared while the history is not. The pin in
`tests/integration/test_prompt_path_sessions.py` gives the second session
its own interaction with the peer. It now asserts that the first session's
trust note reaches the prompt and only one interaction is counted. Against
the old reads, those six pins and all nine prompt-path cases fail. The
`get_trust` calls in `test_epoch_scope.py` and `test_principal_scope.py`
drop `sessions="*"`.

## Slot: merges before EXP-001, by the maintainer's call of 2026-09-22

- **No plan is needed.** Ruling (a) of the
  [sequencing Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens)
  opens no plan before EXP-001 reports. Like
  [#979](https://github.com/mkhomutov/Persatrix/pull/979), this is a
  standalone fix: no store migration, no new setting.
- **Ruling (b)** keeps further memory isolation work off every release after
  v0.3.16 unless an outside ask is recorded. Taking a session filter off a
  read is memory isolation work in its terms, even though it only makes the
  code do what Decision 3 and the RFC 0031 amendments already say. It
  merges as an exception, on the maintainer's call below.
- **EXP-001.** The fix changes nothing any arm sees.
  [`panel.yaml`](../../evaluators/experiments/EXP-001/panel.yaml) sets
  `relationships: []`, so no adviser has a seed. Every meeting is a new
  group channel, and the close path records a relationship interaction only
  for a DM (`record_closed_interaction` in
  `agents/persona_runtime/record_close.py`), so there is no history to
  count. The only relationship rows an adviser can have are identity rows
  written from contact notes. `upsert_identity` tags those `legacy`, and
  identity was already read across sessions.
- **Decision, 2026-09-22.** The maintainer chose to fix it now and merge
  before EXP-001, and chose this fix over seeding under `legacy`.

## Notes

> 2026-09-22 — found in the review of
> [#979](https://github.com/mkhomutov/Persatrix/pull/979), outside that
> PR's scope, and filed with the fix drafted test-first. Also noticed and
> not fixed here: the sessions guide §4 and the MT-SESSION-003
> preconditions say the close path tags a turn's writes with the start-up
> session, so a per-invocation `--session` cannot retag them. Since #459 an
> interaction takes the session bound when it opens, so the override does
> tag them, and the start-up variable applies only to turns with no session
> bound, such as ticks.
>
> 2026-09-22 — **resolved by [#980](https://github.com/mkhomutov/Persatrix/pull/980)**,
> which carries the fix, the tests and this file. It merges before the
> EXP-001 run, by the maintainer's call recorded under Slot.
