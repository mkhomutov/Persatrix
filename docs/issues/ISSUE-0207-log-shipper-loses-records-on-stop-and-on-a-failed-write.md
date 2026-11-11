---
id: ISSUE-0207
summary: "The agent's log shipper loses records in two cases its docstring says are safe. On stop it cancels the stream before the orchestrator has read it, while counting every record as shipped. On a failed write the batch has already left the queue and is counted as neither shipped nor dropped."
status: open
severity: medium
area: agents
created: 2026-11-10
refs:
  - agents/observability/log_shipper.py
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
