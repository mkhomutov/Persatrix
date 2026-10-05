---
id: ISSUE-0183
summary: "Two fact writes that overlap can each supersede the other and leave no live fact. Storing a fact is several steps with nothing serializing them, so when two records closed at the same instant store a fact under one subject and predicate at the same moment, each write marks the other's row superseded and the persona recalls nothing for that key. RFC 0026 says fact writes are serialized per agent; the close path writes them outside the agent lock. Not seen in a run: both inserts must land before either write looks for older rows."
status: open
severity: low
area: memory
created: 2026-10-05
refs:
  - agents/memory/_facts_write.py
  - agents/memory/_facts_supersede.py
  - agents/persona_runtime/finalize_close.py
  - agents/persona_runtime/close_path.py
  - docs/rfcs/0026-declarative-facts-tier.md
  - docs/issues/ISSUE-0180-topic-facts-reachable-only-by-their-exact-subject.md
  - docs/issues/ISSUE-0181-facts-extracted-together-supersede-each-other.md
  - https://github.com/mkhomutov/Persatrix/pull/1034
---

# ISSUE-0183: Two overlapping fact writes can leave no live fact

## Summary

Storing a fact takes several steps: insert the row, look for older live
rows under the same subject and predicate, mark them superseded, commit
([`_facts_write.py`](../../agents/memory/_facts_write.py),
[`_facts_supersede.py`](../../agents/memory/_facts_supersede.py)).
Nothing stops a second write from starting between those steps. When two
writes carry the same time and come from different interaction records,
and both insert before either looks, each finds the other's row and
supersedes it. Both rows end up superseded, and the persona recalls
nothing for that subject and predicate.

## Context

Found on 2026-10-05 while fixing
[ISSUE-0181](ISSUE-0181-facts-extracted-together-supersede-each-other.md).
It behaves the same with and without that fix. Two store calls started
together reproduced it in 100 runs of 100:

```python
store = FactStore(agent_id="a", db_path=":memory:")
await store.initialize()
await asyncio.gather(
    store.store(
        subject="event planning", predicate="topic.decided",
        object="ticket price ceiling is $25",
        source_interaction_id="ix-a", asserted_at=1000.0,
    ),
    store.store(
        subject="event planning", predicate="topic.decided",
        object="lighting crew limited to two people",
        source_interaction_id="ix-b", asserted_at=1000.0,
    ),
)
assert await store.recall(subject="event planning") == []
```

The times must be equal; with different times one row stays live. Equal
times across records are normal: a room-close fan stamps every record it
closes with one instant, and each record's facts are then written by its
own background task, outside the agent lock
([`finalize_close.py`](../../agents/persona_runtime/finalize_close.py),
[`close_path.py`](../../agents/persona_runtime/close_path.py)).

[RFC 0026](../rfcs/0026-declarative-facts-tier.md) names this race under
Security Considerations and says it is handled by serializing fact
writes per agent. Nothing on the close path does that.

## Impact

The persona loses every fact under that subject and predicate until a
later conversation states one again. The window is narrow: both inserts
must land before either write looks for older rows, so the two
extractions have to finish at the same moment and reach the shared key
together. In EXP-001's first practice run, 13 of the 16 sessions in arm
D's four stores held facts from more than one record closed at one
instant, and no two records in a session wrote the same subject and
predicate. Option 1 of
[ISSUE-0180](ISSUE-0180-topic-facts-reachable-only-by-their-exact-subject.md)
would make shared keys routine.

## Proposed fix / investigation path

Test first, with the two gathered calls above expecting one live row.
Then serialize the write, as the RFC already says: one lock per
`FactStore`, held across the insert, the supersession pass and the
commit. What two records closed at one instant should do on a shared key
is a separate choice, decided with ISSUE-0180: today the later arrival
replaces the other record's facts.

## Notes

> 2026-10-05 — filed from the review of the ISSUE-0181 fix. Not slotted:
> whether it is fixed before EXP-001's scored run is the maintainer's
> call.
