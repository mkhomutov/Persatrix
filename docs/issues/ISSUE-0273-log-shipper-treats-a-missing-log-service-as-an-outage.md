---
id: ISSUE-0273
summary: "An orchestrator whose log buffer did not start serves agents without the log service. An agent's log shipper cannot tell that from an outage: it retries for as long as the orchestrator runs, writes a warning with a full traceback to the agent's own log on each failed try, and the records it writes in between are accepted and lost without a line."
status: open
severity: low
area: agents/observability
created: 2026-11-16
refs:
  - agents/observability/log_shipper.py
  - tests/unit/python/test_log_shipper_without_log_service.py
  - cmd/orchestrator/grpcserver.go
  - docs/issues/ISSUE-0204-grpc-server-starts-only-if-the-log-buffer-did.md
  - docs/issues/ISSUE-0246-the-log-shippers-counters-reach-no-metric.md
  - https://github.com/mkhomutov/Persatrix/pull/1135
---

# ISSUE-0273: The log shipper treats a missing log service as an outage

## Summary

Since the fix for
[ISSUE-0204](ISSUE-0204-grpc-server-starts-only-if-the-log-buffer-did.md)
an orchestrator whose log buffer did not start still opens its gRPC port
and serves the wallet on it, without the log service. A log stream to
that port ends at once with the gRPC status `UNIMPLEMENTED`. The shipper
(`agents/observability/log_shipper.py`) handles that status like any
other stream error, as if the orchestrator were briefly away.

## Context

What the shipper does, read in the code and run against a gRPC server
with no log service
(`tests/unit/python/test_log_shipper_without_log_service.py`) and against
a real orchestrator started with a log folder it could not create:

- A record written to the stream is accepted, although the orchestrator
  has already ended the stream. The failure shows only when the next
  record is written. Records written in between are counted as dropped
  when the stream ends, and nothing is logged for them.
- The write that fails does not carry the cause. It raises "RPC already
  finished" or a gRPC `INTERNAL` error; `UNIMPLEMENTED` reaches only the
  task that reads acknowledgements, which nothing looks at until the
  stream is torn down.
- On the failure the shipper logs `log shipper stream error,
  reconnecting in …` at `WARNING`, with the full traceback, waits and
  opens a new stream. The wait starts at 1 second and doubles to 30. A
  failed try never resets it.
- An agent that keeps logging therefore writes one such warning, with
  its traceback, every 30 seconds for as long as the orchestrator runs.
  An agent with nothing to log writes none. Measured against a real
  orchestrator with ten records a second: warnings at 0, 1, 3, 7, 15 and
  31 seconds, then one each 30 seconds; the slowest log call took 0.12
  milliseconds; the shipper's task was still running after 130 seconds.

None of this stops or slows an agent. A log call only appends to a
bounded queue, and the shipper runs as its own task.

## Impact

Noise, and a missing explanation. The warning names a stream error and a
reconnect, which reads as an orchestrator that is restarting. Nothing on
the agent's side says that the orchestrator is up and has no log
service, or that the cure is on the orchestrator (its log folder). The
orchestrator says so itself, at start-up and in `GET /healthz`. The
traceback adds about twenty lines to each warning.

## Proposed fix / investigation path

Tell the cause apart: look at how the acknowledgement reader ended
before reporting a failed write. When it ended with `UNIMPLEMENTED`, log
one line without a traceback that says the orchestrator has no log
service and that records stay in this process's own output, then keep
trying at the longest wait without a line per try, so that an
orchestrator restarted with a working buffer is still picked up. Noticing
the reader's end before the next write would also stop records being
written into a stream that has ended.

## Notes

> 2026-11-16 — found while fixing
> [ISSUE-0204](ISSUE-0204-grpc-server-starts-only-if-the-log-buffer-did.md)
> ([#1135](https://github.com/mkhomutov/Persatrix/pull/1135)).
> Severity proposed from the tracker's precedent
> ([ISSUE-0246](ISSUE-0246-the-log-shippers-counters-reach-no-metric.md),
> low); not ruled by the maintainer. Before that fix the same state left
> the gRPC port closed, and the shipper logged the same warning at the
> same pace for a refused connection, so the fix did not add the noise.
