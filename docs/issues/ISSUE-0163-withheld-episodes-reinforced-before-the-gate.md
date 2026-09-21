---
id: ISSUE-0163
summary: "Under the default `memory.episodic.cross_room: live`, the persona's episodic recall counts a use (`access_count + 1`) of every episode it returns, before the RFC 0037 §D gate, the ISSUE-0132 audience check and the RFC 0017 budget choose what the prompt carries. Uses multiply the ranking score by 1 + ln(1 + uses), so an episode the gate withholds gains one on each turn it ranks in: one use already outranks an equally ranked episode from another room, two beat the 2.0 same-room boost, and it can then hold one of the five recall slots on later matching turns while never reaching the prompt. Episodes the budget drops are counted too, as are the notes tier's and two other episodic reads' rows. The facts tier counts only what its budget admitted. A test-first fix for the live read is drafted and held: it changes an EXP-001 arm."
status: open
severity: medium
area: memory
created: 2026-09-18
refs:
  - https://github.com/mkhomutov/Persatrix/pull/970
  - agents/persona_runtime/memory_context.py
  - agents/memory/episodic_room_ranked.py
  - agents/memory/episodic.py
  - agents/memory/migrations.py
  - agents/memory/_session_filter.py
  - agents/memory/scope_recall.py
  - agents/persona_runtime/channel_history.py
  - agents/persona_runtime/memory_assembly.py
  - agents/memory/notes.py
  - docs/memory-quality-roadmap.md
  - tests/unit/python/test_cross_room_live.py
  - docs/rfcs/0049-amendment-l1-cross-room-availability.md
  - docs/issues/ISSUE-0132-memory-egress-gate-blind-to-room-audience.md
  - docs/experiments/EXP-001-preregistration.md
---

# ISSUE-0163: The live episodic recall counts withheld episodes as used — they climb the ranking and can hold recall slots they never fill

## Summary

On every turn the persona recalls the five episodes that best match the
turn, and the RFC 0037 §D gate then withholds any the room may not hear.
Since v0.3.16 that includes any episode above `public` whose room did not
hold everyone in this one ([ISSUE-0132](ISSUE-0132-memory-egress-gate-blind-to-room-audience.md));
a `public` episode is shareable by definition and stays.
The recall also counts a use of every episode it returned, and each use
raises that episode's ranking score on every later turn. So an episode the gate
withholds, which never reaches the prompt, is counted as used on each turn
it ranks in. It can climb above episodes the persona could have used and
keep one of the five slots for itself. Measured on the real prompt path at the
shipped defaults: from the third matching turn on, the prompt carried four
episodes instead of five, and the withheld episode's count kept rising.

## Context

**Where.** Under the default `memory.episodic.cross_room: live`,
[`_inject_memory_context`](../../agents/persona_runtime/memory_context.py)
reads episodes with `recall_room_ranked(..., reinforce=True)`.
[`recall_room_ranked`](../../agents/memory/episodic_room_ranked.py) then runs
`UPDATE episodes SET access_count = access_count + 1, last_accessed_at = ?`
on every row it returns. Only after that does the method apply
`gate.filter_entries("episodic", ...)`, the §D rank check with the audience
check beside it, and then the RFC 0017 budget.

**Why a use matters.** Every branch of the episodic ranking (FTS5, LIKE,
recency) multiplies the score by `(1 + ln(1 + access_count))`
(`_SCORE_TEMPLATE` in [`migrations.py`](../../agents/memory/migrations.py)).
Episodes from the acting room get `ROOM_BOOST_FACTOR = 2.0`
([`_session_filter.py`](../../agents/memory/_session_filter.py)). Two
unearned uses give 1 + ln 3 ≈ 2.10, more than the boost, so a withheld
episode from another room then outranks an equally relevant same-room
episode that has not been used.

**What it contradicts.**
- The stated intent. The `episodic_room_ranked.py` module docstring and the
  [RFC 0049 L1 amendment's Promotion section](../rfcs/0049-amendment-l1-cross-room-availability.md#promotion-v0312-pr-4--the-measurement-gated-flip)
  justified the bump with "a recalled-and-used episode is a used episode
  wherever it was formed". A withheld episode is recalled, not used.
- The facts tier. [`memory_assembly.py`](../../agents/persona_runtime/memory_assembly.py)
  reinforces only the facts the budget admitted,
  `mark_recalled(budget.admissions_by_tier("facts"))`, and the facts
  renderer drops a subject's pending admissions when its header does not
  fit, so the write never lands on a fact the prompt did not carry.

**Measured** at `8ad2d7e4` with the real `_MemoryContextMixin` and a real
`EpisodicMemory`, at the shipped defaults (`cross_room: live`,
`audience: live`), acting at `internal` in `group:planning`, whose members
are Alice, Bob and the persona. The store holds one withheld episode,
"atlas zephyr retro" (importance 0.4, from another room), and five
admissible ones, "atlas retro note-0" to "note-4" (importance 0.5, from a
third room). Two turns match only the withheld episode; the next three
match all six. Neither side of this store carries the same-room boost, so
one unearned use is already enough to crowd an admissible episode out here;
two are what it takes against a same-room episode.

| Turn | Query | Withheld episode's uses | Episodes in the prompt | The five admissible episodes' uses |
|---|---|---|---|---|
| 1 | `zephyr` | 1 | 0 | 0, 0, 0, 0, 0 |
| 2 | `zephyr` | 2 | 0 | 0, 0, 0, 0, 0 |
| 3 | `atlas` | 3 | 4 | 0, 1, 1, 1, 1 |
| 4 | `atlas` | 4 | 4 | 0, 2, 2, 2, 2 |
| 5 | `atlas` | 5 | 4 | 0, 3, 3, 3, 3 |

The numbers are the same for both reasons the gate withholds: a
`restricted` episode on an `internal` turn, and an `internal` episode from a
DM with Alice alone, withheld because Bob is in the room. "note-0" never
reaches the prompt and never gains a use, so it is crowded out for good.
Episodes the budget drops are counted the same way: with room in the budget
for one of two episodes the gate passed, both gained a use.

**The same pattern on three other reads.** None of them is the default
episodic read. `test_recall_increments_access_count` and
`test_access_count_persists_in_db` in `test_episodic_memory_core.py` pin the
walled read's bump, so a fix there changes them; nothing pins the other two.
- The walled read, `EpisodicMemory.recall` under `cross_room: off` or
  `shadow`, counts a use of every row it returns before the gate. Its rows
  come from the acting room, so the gate withholds fewer of them, but budget
  drops still count.
- The notes tier, on this same prompt path:
  [`recall_notes_for_event`](../../agents/persona_runtime/notes_section.py)
  runs `UPDATE notes SET access_count = access_count + 1`
  ([`notes.py`](../../agents/memory/notes.py)) on every note it returns, and
  the gate and the budget come after. Notes rank without a use term, so the
  count does not feed their ranking — but the eviction sweep prunes
  `ORDER BY access_count ASC`, so a note the gate withheld or the budget
  dropped outlives one that was never recalled.
- The channel-history recall
  ([`recall_channel_episodes`](../../agents/persona_runtime/channel_history.py)
  through [`recall_with_scope_filter`](../../agents/memory/scope_recall.py))
  reads up to 60 of the session's most recent episodes with
  `EpisodicMemory.recall` on every channel message, counting a use of each,
  then keeps the ones in the turn's scope, at most 20; the gate and the
  budget come after. In a run like the one above but with the five
  admissible episodes stored in the acting room, each gained a use on every
  channel message — one on the `zephyr` turns they did not match, two on the
  `atlas` turns, where the live ranked read bumped them again. This path
  runs under the default too.

Outside the prompt path the shared-pool read does the same, and the repo
already records it: `SharedMemoryPool.read` bumps every BM25 hit, the rows
its `min_confidence` filter then drops included — the inline comment in
[`shared_pool.py`](../../agents/memory/shared_pool.py) and NTH-1-access-count
in the [RFC 0008 PR plan](../rfcs/0008-pr-plan.md).

The withheld case in `test_cross_room_live.py` checks the prompt and the
manifest, not the count.

## Impact

- **Recall quality under the default configuration, not confidentiality.**
  Nothing withheld reaches the prompt. But the room layout ISSUE-0132 exists
  for, a DM with one person and a group room with more, now withholds
  routinely. A DM episode that matches group-room turns can take a recall
  slot there from an admissible episode with fewer uses, one from a third
  room or a same-room episode older than the 60 the channel-history recall
  reads, and keep it: every matching turn adds another use.
- **The counts are wrong wherever the episode ranks.** A DM episode counted
  as used by group-room turns also ranks higher back in the DM, on uses that
  never happened. Nothing repairs the counts already written: `access_count`
  only rises, and ruling (f) of the Amendment 2026-09-12 bars a new store
  migration before EXP-001 reports, so a store running since the v0.3.12
  flip keeps the advantage it accumulated even after the fix lands.
- **Not new in v0.3.16.** The live recall has counted this way since RFC
  0049 PR 4 (v0.3.12), when classification was the only reason to withhold.
  The audience check made withholding common.

## Proposed fix

Apply the facts tier's rule to the live recall: read with
`reinforce=False`, and after the allocate-loop count a use of each episode
in `budget.admissions_by_tier("episodic")`, the ones the prompt carried. A
§E projection served in place of a withheld episode is admitted under that
episode's id, so it counts as a use of it. This is
[Memory Quality Roadmap §C](../memory-quality-roadmap.md#c-salience-score-with-use-based-reinforcement)'s
rule — reinforcement lands on what the allocator admitted, not on what it
dropped — applied early to one read; §C still owns the salience formula and
its own `last_recalled_at` column, folded into the
[RFC 0008 calibration review](../rfcs/0008-calibration-review.md).

Leave the walled read, the notes tier and the channel-history recall as they
are for now: changing them moves the ranking under `off` and `shadow`, the
eviction order for notes, and something on every channel message, and each
needs its own decision. So the rule above holds for the live ranked read
alone. Under the default the channel-history read still counts what the
prompt did not carry: an admitted same-room episode gains two uses on a
matching turn, and a same-room episode the budget drops still gains one. The
deeper change is the tier-agnostic one — every prompt-path read
non-reinforcing, one reinforcement from `budget.admissions_by_tier(tier)`
for every tier and mode — and it is what these deferred reads add up to.

Drafted test-first as draft PR [#972](https://github.com/mkhomutov/Persatrix/pull/972):
- tests: a withheld episode's count is unchanged after a turn, for both
  reasons; an episode the budget drops is unchanged; after the two `zephyr`
  turns above, all five admissible episodes reach the prompt; and under
  `off`, on a mention turn, an admitted episode is counted once, not twice
  (on a channel message the channel-history recall bumps the same row as
  well, so the pair is counted there whatever this fix does). The first
  three fail at `8ad2d7e4` and pass with the fix;
- `EpisodicMemory.reinforce(ids)`, the same UPDATE scoped to the agent,
  called after the allocate-loop under `live` only;
- with the fix, the table above reads 0 uses for the withheld episode and
  five episodes in the prompt from turn 3;
- `make eval-replay`: 6/6 recipes before and after. The two reports differ
  only in per-run episode and fact ids, which also differ between two runs
  of the same code, so no RFC 0044 golden moves.

## Slot: not slotted, held behind EXP-001

- Ruling (a) of the [sequencing Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens)
  opens no plan before EXP-001 reports and the first strategy review logs
  its result.
- Ruling (b): no further memory isolation, attribution or audience work
  boards a release after v0.3.16 unless an amendment's External evidence
  section records an outside ask for it. The same ruling keeps the RFC 0037
  §D gate as built, and this fix leaves it there: it changes neither the
  gate nor the audience check, only what the allocator's admissions do to
  episodic ranking. So ruling (b) is the neighbourhood, not the hold.
- EXP-001's arm D runs memory "as shipped, through the 1 500-token
  allocator" ([pre-registration §2](../experiments/EXP-001-preregistration.md#2-the-arms)),
  frozen when [#969](https://github.com/mkhomutov/Persatrix/pull/969)
  merged. The fix changes which episodes count as used even when nothing is
  withheld, because budget drops stop counting, so merging it before the
  run would change arm D.
- [#913](https://github.com/mkhomutov/Persatrix/pull/913) and
  [#963](https://github.com/mkhomutov/Persatrix/pull/963) merged after the
  v0.3.16 tag because they needed no plan. This fix is different: it
  changes an EXP-001 arm. The amendment that follows the first strategy
  review decides; #972 waits for it.

## Notes

> 2026-09-18 — found in the review of
> [#970](https://github.com/mkhomutov/Persatrix/pull/970), outside that
> PR's comment-only scope; confirmed with the measurement above and filed
> with the fix drafted test-first and held (#972). The
> `episodic_room_ranked.py` docstring, the test that pins the bump, the
> live call site, the persona-agents guide and the RFC 0049 L1 amendment
> now say what the recall counts and point here.
