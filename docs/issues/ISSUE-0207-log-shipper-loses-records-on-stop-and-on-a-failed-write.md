---
id: ISSUE-0207
summary: "The agent's log shipper loses records in two cases its docstring says are safe. On stop it cancels the stream before the orchestrator has read it, while counting every record as shipped. On a failed write the batch has already left the queue and is counted as neither shipped nor dropped."
status: resolved
severity: medium
area: agents
created: 2026-11-10
closed: 2026-11-14
closed_pr: 1115
refs:
  - agents/observability/log_shipper.py
  - agents/observability/log_record_proto.py
  - tests/unit/python/test_log_shipper_counts_every_record.py
  - docs/issues/ISSUE-0246-the-log-shippers-counters-reach-no-metric.md
  - agents/server.py
  - docs/rfcs/0018-structured-logging-framework.md
---

# ISSUE-0207: The log shipper loses records on stop and on a failed write

## Summary

`agents/observability/log_shipper.py` sends the agent's log records to
the orchestrator over one gRPC stream. Its module docstring makes two
promises that do not hold when run:

- "Records enqueued during the gap stay in the queue (subject to the
  FIFO drop)."
- "`Shipper.stop` drains the queue and awaits the final ack from the
  orchestrator before returning."

## Context

**Stop.** After `done_writing()` the `finally` block cancels the task
that reads acknowledgements. In grpc.aio, cancelling a pending read
cancels the whole call. Against an in-process log service that takes
20 ms per batch, the server received 256 of 2 000 records in one run,
and 0 and 1 in two later runs; how many arrive depends on timing. Each
time it saw the stream end as cancelled, while `shipped` read 2 000 and
`last_ack_seq` read 0. `AgentServer.stop()` relies on this path to deliver the logs of
a shutdown. How much the Go server loses depends on how far its receive
loop got; that was not run.

**Failed write.** The request generator pops a batch of up to 256 records
from the queue and yields it. `shipped` is increased only when the
generator is resumed for the next batch. If `call.write` fails, the
generator is never resumed: the batch is gone and neither `shipped` nor
`dropped` moves. With the orchestrator unreachable, 768 of 1 000 queued
records vanished in three attempts over 4.5 seconds.

## Impact

The orchestrator's copy of the log loses the records wanted most: the
last lines before a stop and the lines written while the orchestrator was
unreachable. The counters that should reveal the loss say nothing was
lost. Every record still reaches the agent's own stderr.

## Proposed fix / investigation path

On stop, wait for the acknowledgement reader to reach the end of the
stream, bounded by the stop timeout, before cancelling it. On a failed
write, put the batch back at the front of the queue or count it as
dropped. Putting it back needs one more change: once the stop flag is
set the reconnect wait returns at once, so a stop against an unreachable
orchestrator would retry in a tight loop until its timeout. Count what is
still queued as dropped at that point.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; both cases reproduced with scratch probes against an
> in-process log service.

> 2026-11-13 — fixed, following the proposed fix. Checked against the
> code at `dd006cba` first, with both measurements as tests: a log
> service that takes 20 ms a batch received 0 of 2 000 records in three
> runs of a stop, and with a write that fails 768 of 1 000 records were
> gone from the queue after three attempts. The Summary and Context hold
> as written.
>
> - **Stop.** After the last batch the stream is half-closed and the
>   shipper waits for the acknowledgement reader to reach the end of
>   the stream, which is when the orchestrator has read everything. The
>   wait is bounded by `stop()`'s timeout.
> - **A failed write.** The batch goes back to the front of the queue,
>   in order. The queue keeps its bound: what that evicts is counted.
> - **The tight loop.** Once stopping, a failed attempt is the last: no
>   reconnect. What is still queued is counted as dropped.
>
> Two things follow from making the counters say what happened, and go
> a step past the proposed fix:
>
> - `shipped` now counts records the orchestrator has **acknowledged**,
>   not records written. A record written to a stream that ends before
>   it is acknowledged is counted as `dropped`. So after `stop()`,
>   `enqueued == shipped + dropped`, whatever happened in between. The
>   orchestrator acknowledges after a batch once 32 records have come
>   in since its last acknowledgement, and at the end of a stream. So a
>   stream that breaks can count as dropped some records the
>   orchestrator did receive: up to 31, and a batch it had not answered
>   yet. Such records are not sent again: RFC 0018 chose "no replay"
>   for what has left the shipper.
> - A record that cannot be encoded is counted as dropped and left out
>   of its batch. Put back with a failed batch it would have failed
>   every later stream; before, it broke the stream and vanished with
>   its batch.
>
> `stop()` logs one WARNING with the number of records not delivered,
> when there are any. That line is the only place the count is shown:
> the counters reach no metric, which is
> [ISSUE-0246](ISSUE-0246-the-log-shippers-counters-reach-no-metric.md).
> The record-to-wire conversion moved to
> `agents/observability/log_record_proto.py`, to keep `log_shipper.py`
> under the 500-line warning.

> 2026-11-14 — review of the fix. One finding was a new cost of the
> fix itself.
>
> - **Memory grew through an outage.** Each failed attempt logs a
>   warning with its traceback, and the agent's logging chain hands
>   that record to the shipper like any other. The traceback holds the
>   frame of the failed attempt, and so the whole batch it had encoded.
>   Before the fix such a record was lost with the next failed batch;
>   with batches going back to the queue they piled up, one batch kept
>   alive per attempt. A queued record no longer keeps `exc_info` or the
>   logging record; neither was ever sent. Run with the real logging
>   chain against a closed port, 256 queued records of about 500 bytes
>   and 400 failed attempts: 400 batches alive and 162 MiB before, one
>   batch and 78 MiB after.
> - **"Shipped or dropped" did not hold on three paths**: a record
>   handed over after the stop, one arriving from another thread while
>   the stop ran, and a stop before any start. A stopped shipper now
>   counts a late record as dropped, and a stop that was never started
>   counts what was queued.
> - **The shipper counted its own stop warning.** The line it logs when
>   a stream fails during a stop was queued into the shipper that was
>   stopping, so three undelivered records were reported as "4 of 4".
>   The process-wide handle is now cleared before the stop does anything
>   else.
> - **"Not delivered" said too much.** The count is records the
>   orchestrator did not acknowledge; it acknowledges every 32, so some
>   of them arrived. The warning and the texts say "not acknowledged".
> - The stop's one attempt uses the connection as it is. Straight after
>   an outage the connection is still waiting to retry, the attempt
>   fails at once, and what was queued through the outage is not sent.
>   Left as it is, and now stated: waiting for the connection would hold
>   every stop for the full timeout whenever the orchestrator is down.
> - [ISSUE-0246](ISSUE-0246-the-log-shippers-counters-reach-no-metric.md)
>   said no metric instrument for dropped records existed. One does and
>   is never recorded; that issue is corrected.
