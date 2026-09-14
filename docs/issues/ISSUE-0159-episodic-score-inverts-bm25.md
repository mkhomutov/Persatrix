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
  - docs/rfcs/0017-persona-memory-quality.md
  - agents/memory/episodic_queries.py
  - agents/memory/episodic_room_ranked.py
  - agents/persona_runtime/memory_context.py
  - agents/persona_runtime/tripwire_watch.py
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

## Notes

> 2026-09-15 — filed from the v0.3.16 release-prep arc (F-3 in the execution
> report). The MT's Leg 4 is recorded *inconclusive* citing this issue; the
> deterministic firing stays pinned in
> `tests/integration/test_confidentiality_tripwire.py`.
