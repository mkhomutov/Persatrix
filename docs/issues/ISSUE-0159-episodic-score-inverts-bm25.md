---
id: ISSUE-0159
summary: "Episodic recall scores an FTS5 match as 1/(1+|rank|) and drops it below min_score 0.20 — but FTS5's bm25 rank grows more negative the MORE of the summary a stimulus matches, so a one-word mention scores ~1.0 and a stimulus that quotes the summary scores ~0.04 and is discarded. The episodic tier therefore surfaces an episode only on thin, one-or-two-term mentions and never on the turns that engage with its content; the §G confidentiality tripwire, which watches withheld episode candidates, cannot be reached live by MT-PERSONA-CONFIDENTIALITY-001 Leg 4 because the seeded echo is exactly the stimulus the score discards."
status: open
severity: medium
area: memory
created: 2026-09-15
refs:
  - docs/manual-tests/v0.3.16-execution-report.md
  - docs/manual-tests/MT-PERSONA-CONFIDENTIALITY-001.md
  - docs/manual-tests/MT-MEMORY-CROSSROOM-001.md
  - docs/rfcs/0017-persona-memory-injection-budget.md
  - agents/memory/episodic_queries.py
  - agents/memory/episodic_room_ranked.py
  - agents/memory/_fts5_query.py
  - agents/memory/_notes_recall.py
  - agents/persona_runtime/memory_context.py
  - agents/persona_runtime/tripwire_watch.py
  - docs/experiments/EXP-001-harness.md
  - docs/issues/ISSUE-0163-withheld-episodes-reinforced-before-the-gate.md
---

# ISSUE-0159: Episodic recall's score inverts BM25 — the better the match, the less likely the episode is recalled

## Summary

[`recall_fts5`](../../agents/memory/episodic_queries.py) filters candidates
with `(1.0 / (1.0 + ABS(fts.rank))) >= min_score` and the production tier
passes `DEFAULT_EPISODIC_MIN_SCORE = 0.20`. FTS5's `rank` is bm25, negative,
and its magnitude grows with how much of the document the query matches. So
the filter admits `|rank| <= 4` only: a stimulus that mentions one stored
term scores ~1.0 and passes; a stimulus that overlaps the summary heavily
scores a few hundredths and is discarded. Measured on the live store at the
v0.3.16 arc: `zephyr` → rank −0.000001, score 1.0; the episode's own summary
as the stimulus → rank −23.05, score 0.04, **dropped**. The query is also the
sanitised stimulus verbatim, an implicit AND over every term, so any natural
sentence with a word the summary lacks matches nothing at all.

## Context

Found while executing [MT-PERSONA-CONFIDENTIALITY-001](../manual-tests/MT-PERSONA-CONFIDENTIALITY-001.md)
Leg 4 at the v0.3.16 release-prep arc ([execution report](../manual-tests/v0.3.16-execution-report.md)).
The leg seeds a verbatim leak of the `restricted` episode summary into an
`internal` room so the §G tripwire — which watches the turn's **withheld**
candidates ([`tripwire_watch.py`](../../agents/persona_runtime/tripwire_watch.py))
— has something to fire on. It never can: the seeded text is the summary, the
summary is the strongest possible match, and the score discards it, so the
episode is not a candidate, is not withheld, is not watched, and an echo of
forty verbatim words fired nothing (audit lines 0, metric absent). The
v0.3.12 run reached the same "inconclusive" reading without seeing why.
[MT-MEMORY-CROSSROOM-001](../manual-tests/MT-MEMORY-CROSSROOM-001.md) already
notes that "FTS relevance on natural channel turns rarely surfaces a
cross-room episodic delta" — this is the mechanism.

## Impact

The episodic tier contributes on thin mentions and goes quiet on the turns
that actually engage an episode's content, which is the opposite of what a
relevance threshold is for. The §G tripwire's live observability cannot be
demonstrated through the MT as written. Not a v0.3.16 regression — the formula
predates RFC 0037.

## Proposed fix / investigation path

Normalise bm25 into a monotone [0, 1] relevance (FTS5 exposes the per-query
best rank; `1 - rank/best_rank`, or a sigmoid on `-rank`), or drop the
threshold for FTS matches and keep `min_score` for the LIKE fallback only;
consider OR-ing the query terms (or a prefix of the sanitised stimulus) so a
natural sentence can match. Re-record whichever goldens shift, and give the
MT's Leg 4 a seed that survives the fixed filter.

## Fix

The query side changes; the store does not (no tokenizer, table or trigger
change). Episodic and notes recall share one MATCH builder,
[`_fts5_query.py`](../../agents/memory/_fts5_query.py):

- **Any shared word matches.** Each word of the message becomes one quoted
  phrase of its letters and digits, and the phrases are joined with OR, so
  an identifier such as `unique-alpha-xyzzy` stays one phrase. Common words
  are trimmed from the end of a word, a lone single character is dropped,
  and only the first 40 phrases are searched. A message made only of common
  words is searched as written. A quoted phrase cannot be FTS5 syntax, so
  `NOT` is searched as a word and no message raises a syntax error. Text
  with no letter or digit, such as `*`, keeps each tier's old path: recency
  for episodes, a LIKE search for notes.
- **System words do not count for episodes.** Every closed conversation
  stores its bookkeeping in the searched context column (`close_reason`,
  `participant_type`, `channel_message`, …), and every single-turn event
  stores `Event: … → Actions: […]` as its summary. An agent's message
  reaches recall as "Message from X: …", so the word "message" alone would
  bring back unrelated episodes and count each as used. Episode search
  trims those words like common words; a test pins the list to the
  runtime's event types, action types and close reasons, and another drives
  a real persona and checks every word it stored is listed. A task's own
  words still match, because they live only in the stored event.
- **The floor is relative.** A row stays when its bm25 relevance is at
  least `min_score` times the best relevance among the candidates, the rows
  that pass the call's own filters (agent, importance, session wall,
  principal, epoch, and for notes the protection levels). A fixed floor
  would empty small stores: FTS5 gives a word found in half the rows or
  more almost no weight, so in a store of one or two rows every bm25 is
  about 1e-6. The defaults stay 0.20; `1.0` now keeps the best match and
  its ties.
- **A withheld episode does not set the bar.** The §D gate judges episodes
  after the search, so the persona passes the levels its turn may inject
  and only those rows set the best. Otherwise a restricted episode could
  push every admissible one below the floor, and its absence from the
  prompt would hint that it exists. The withheld rows stay candidates: the
  gate's §E projection branch serves cleared-down stand-ins for them.
- **A tick searches nothing.** OR matching would let the tick sentence
  match ordinary rows and end the RFC 0017 §F short-circuit, one model call
  per tick in a public room. The builder returns no query for that exact
  sentence; a second copy of it lives in the memory package, pinned to the
  persona runtime's by a test.

Measured on a replay of EXP-001's first practice run, each meeting replayed
against the store it started with, the briefing reaches 23 of arm D's 25
speaking turns in the three later meetings, every opening turn included,
where the shipped code reached none. That replay predates the system-word
and withheld-bar changes above, and runs again on them before the scored
run. Three goldens gain the
episode the fix recalls (EVAL-MEMORY-003, 004 and 005, re-recorded offline);
EVAL-MEMORY-005 now judges the DM-taught episode beside the fact.

## Slot: merges before EXP-001, by the maintainer's call of 2026-10-02

- **No plan is needed.** Ruling (a) of the
  [sequencing Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens)
  opens no plan before EXP-001 reports; like
  [#1014](https://github.com/mkhomutov/Persatrix/pull/1014), this is a
  standalone fix. Ruling (f) holds: no store migration.
- **Ruling (b)** covers memory isolation, attribution and audience work.
  This is recall relevance: the §D gate and the audience check are
  unchanged, and they now judge more candidates.
- **EXP-001.** Arm D changes most: it is the only arm whose prompt carries
  injected memory. The `recall_notes` tool changes for every arm that has
  it, B, C, D and D′ alike: it now finds a note that shares any word with
  the query. A has no runtime memory. Check 2 of the pre-registration
  needs a briefing fact to reach a later meeting's prompt through the
  shipped memory path, and with the shipped search it never could. Unlike
  [ISSUE-0163](ISSUE-0163-withheld-episodes-reinforced-before-the-gate.md),
  which only re-ranks what already carries, this decides whether anything
  carries at all.
- **Decision, 2026-10-02.** The maintainer ruled that arm D runs the memory
  code as shipped when the scored run happens, as #979, #980 and #1014
  were treated, so this fix lands before it. The same call unholds #972
  (ISSUE-0163), whose reinforcement now runs on nearly every turn, and
  has the facts tier's two faults, ISSUE-0180 and ISSUE-0181, fixed before
  the scored run too.

## Notes

> 2026-09-15 — filed from the v0.3.16 release-prep arc (F-3 in the execution
> report). The MT's Leg 4 is recorded *inconclusive* citing this issue; the
> deterministic firing stays pinned in
> `tests/integration/test_confidentiality_tripwire.py`.

> 2026-10-02 — found again by EXP-001's first paid practice run: arm D
> stored its briefing in every adviser's memory and recalled none of it in
> the three later meetings, answering "the panel does not know" to all three
> recall questions. Replaying the practice messages against the advisers'
> stores returned no episode for any of them. Leg 4 of
> MT-PERSONA-CONFIDENTIALITY-001 is reachable with the fix (v1.3) and has
> not been re-run live.

> 2026-10-02 — review of the fix. Two gaps closed in the same change: the
> system words above, and the withheld bar. Known limits it leaves: an
> episode the audience check withholds can still set the bar, because the
> audience is known only after the search; withheld rows still take places
> under the search's row limit, as they did before; bm25 weighs each word
> over the whole table, every agent, tenant and level included, so the
> filters choose the candidates but not the weights; and a sender's name
> still matches the episodes that sender took part in.
