---
id: ISSUE-0189
summary: "In v0.4.0 a DecisionRecord (RFC 0028) lives only where its agent runs: in that agent's own decision log file, rotated at a size cap, and in sampled spans. The orchestrator never receives one, and no society table holds them, so nobody can ask what an organization decided across its members without collecting each agent's file, and a record is gone once its file rotates out or its host's disk is lost. RFC 0029 Phase 5, which puts decision records in the society store, stays unslotted. A Known Gap of the v0.4.0 plan."
status: open
severity: low
area: decision
created: 2026-10-23
refs:
  - docs/rfcs/0028-agent-decision-policy-engine.md
  - docs/rfcs/0028-pr-plan.md
  - docs/rfcs/0029-personal-society-storage-split.md
  - docs/v0.4.0-plan.md
  - agents/decision/decision_types.py
---

# ISSUE-0189: Decision records are runtime-local

## Summary

Every choice an agent's decision checkpoint makes is written down as a
`DecisionRecord`, but in v0.4.0 the record stays in the agent that made it.
There is no place where the records of a whole organization can be read
together, and each agent keeps only as many as its log rotation allows.

## Context

RFC 0028 §D gives a record two sinks, both inside the agent: a span under
the agent's tracer, which is sampled, and the decision log,
`agents/observability/decision_log.py`, one JSON line per record. The PR
plan's E-10 makes the log one file per agent, rotated at a size cap with a
set number of rotated files kept (PR 3). Open Question 4 is resolved as no
protobuf decision event in v0.4.0, and the RFC 0029 amendment that put the
society state in the SQLite channels store keeps decision records out of it,
so RFC 0029 Phase 5 — the `decision_records` society table — stays
unslotted. The v0.4.0 plan lists this as a Known Gap, owned by row B1, which
files this issue with the decision types (RFC 0028 PR plan, PR 1).

## Impact

- An operator who wants to know what an organization decided, or why two
  members acted differently on one directive, has to gather every member's
  log file by hand.
- A record lasts only as long as its log file: rotation drops the oldest,
  and losing the agent's disk loses them all. The replay harness (PR 8) can
  replay only what is still on disk.
- RFC 0028 Phase 4's collective decisions (v0.5.0+) need records from many
  agents in one place; they cannot be built on this.

None of this weakens a gate: approvals and their tokens live in the society
tables (Phase 2a), not in decision records.

## Proposed fix / investigation path

RFC 0029 Phase 5: a `decision_records` table in the society store, written
from the agent through the society-store interface, with the decision log
kept as the agent-side copy. It needs a slot, and a decision on the wire
format Open Question 4 deferred.

## Notes

> 2026-10-23 — filed by RFC 0028 PR plan PR 1, as the v0.4.0 plan's Known
> Gaps table assigns to row B1.
