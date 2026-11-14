---
id: ISSUE-0246
summary: "The agent's log shipper counts the records it enqueues, delivers and drops, and nothing reads the counts: they are attributes of an object, not a metric. The counter the docs say is exported for dropped log records (`agent.observability.logs.dropped`) exists and is never recorded, so an operator learns of lost log records only from one WARNING when the agent stops."
status: open
severity: low
area: agents/observability
created: 2026-11-13
refs:
  - agents/observability/log_shipper.py
  - agents/observability/metrics.py
  - docs/rfcs/0018-structured-logging-framework.md
  - docs/issues/ISSUE-0207-log-shipper-loses-records-on-stop-and-on-a-failed-write.md
---

# ISSUE-0246: The log shipper's counters reach no metric

## Summary

`Shipper` (`agents/observability/log_shipper.py`) keeps four counters:
`enqueued`, `shipped`, `dropped` and `last_ack_seq`. Its own comment
says they are "surfaced for tests and (future) metric instruments".
Nothing reads `Shipper.dropped`. The instrument for it exists and is
never recorded: `agents/observability/metrics.py` defines
`logs_dropped` (`agent.observability.logs.dropped`, "Reserved here;
emitted by RFC 0018 PR 5"), and only a test ever adds to it.
`docs/observability.md` and RFC 0019 list it as an exported drop
counter with a `reason` attribute, which is not true yet.

## Context

- The fix for
  [ISSUE-0207](ISSUE-0207-log-shipper-loses-records-on-stop-and-on-a-failed-write.md)
  made the counters true: after a stop, every record is either shipped
  or dropped. It also added the one place a count is shown, a WARNING
  when the shipper stops with records not delivered.
- While the agent runs, nothing shows a drop. A queue that overflows
  for hours, or an orchestrator that is unreachable, leaves no signal
  on any dashboard.
- Every record still reaches the agent's own log output, so no log line
  is lost outright: what is lost is the orchestrator's copy.

Read from the code.

## Impact

An operator cannot see, alert on or chart lost log records while an
agent runs. Proposed low: the records are still in the agent's own
output, and the stop-time WARNING now gives the total. The severity is
proposed here, not ruled.

## Proposed fix / investigation path

Record the existing `logs_dropped` counter from the shipper, with the
`reason` the docs name (a full queue, an unacknowledged write, a stop,
a record that cannot be encoded), and do not add a second drop counter.
Counters for enqueued and shipped records would be new. The shipper's
counters only go up, so they map onto monotonic counters as they are.
`enqueue()` runs on any thread and must not block: record from the
drain task, by the difference since the last record, not from the hot
path.

## Notes

> 2026-11-13 — noticed while fixing
> [ISSUE-0207](ISSUE-0207-log-shipper-loses-records-on-stop-and-on-a-failed-write.md),
> which says the counters "should reveal the loss".

> 2026-11-14 — corrected in review of the ISSUE-0207 fix. This issue
> first said no instrument existed. One does, `logs_dropped`; it is
> never recorded, and two docs say it is exported.
