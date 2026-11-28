---
id: ISSUE-0284
summary: "An agent's gRPC server hands its code a metadata value of any size when the client sends its call before acknowledging the server's settings: two million characters got through, where a client that waits is stopped at 16,384. The logging interceptor then copies four such values (the `persatrix-*` ids) into every log record written during the call, with no limit on length: one failing call with a 300,000-character id wrote a 300,348-character record."
status: open
severity: low
area: agents
created: 2026-11-21
refs:
  - agents/server.py
  - agents/server_cli.py
  - agents/observability/grpc_logging.py
  - agents/observability/logging.py
  - docker-compose.yaml
  - docs/issues/ISSUE-0153-compose-ports-open-on-every-interface.md
  - docs/issues/ISSUE-0265-merged-log-view-reads-every-finished-run-from-disk.md
  - https://github.com/mkhomutov/Persatrix/pull/1138
---

# ISSUE-0284: An agent's gRPC server takes a metadata value of any size, and its logs repeat four of them whole

## Summary

Every gRPC call carries metadata: short named values beside the request,
such as the trace id and the four `persatrix-*` ids that tie a log line
to its run. gRPC limits how large they may be. An agent's server applies
that limit only to a client that plays by the rules. A client that sends
its call at once, before it has acknowledged the server's settings, gets
a value of any size through to the agent's code.

One piece of that code repeats what it is given. The logging interceptor
copies four metadata values into every log record written during the
call, whatever their length.

## Context

Found while measuring whether CVE-2026-126360, a slow read of one
malformed `traceparent` value, can reach an agent (pull request 1138).
That advisory is fixed by its own pin move. What is left is the
size of what a caller can hand an agent.

**The server.** `agents/server.py` builds it with
`grpc.aio.server(interceptors=[LoggingMetadataInterceptor()])` and no
options, so gRPC's default limits apply. Measured on a server built that
way, with a hand-built HTTP/2 client, one metadata value per call:

| The client | Length of the value | Result |
|---|---|---|
| waits for the server's settings and acknowledges them | 7,058 | reaches the agent's code |
| the same | 20,058 | refused: `received metadata size exceeds hard limit (value length 20058 vs. 16384)` |
| sends at once | 17,057 | reaches the agent's code, three tries of three |
| sends at once | 300,057 | reaches the agent's code, three tries of three |
| sends at once | 2,000,057 | reaches the agent's code, five tries of six; refused once |

The same at the old and at the new OpenTelemetry pins. The numbers fit
one reading: the limit starts to hold when the client acknowledges the
settings, and the one refusal is a call that lost that race. gRPC's own
source was not read to confirm it.

**The logs.** `_bind_from_metadata` in
`agents/observability/grpc_logging.py` reads `persatrix-execution-id`,
`persatrix-step-id`, `persatrix-agent-id` and `persatrix-workflow-id`
and binds each value as it is, so every log record written inside the
call carries them. Measured with a task call whose model call fails, so
that the handler writes one `ERROR` record:

| Metadata sent | Length | The log record |
|---|---|---|
| `persatrix-execution-id` | 12 | 360 characters |
| `persatrix-execution-id` | 300,000 | 300,348 characters |
| `persatrix-workflow-id` | 300,000 | 300,347 characters |
| a name that is not one of the four | 300,000 | 328 characters: not copied |

A task call that succeeds wrote no record at the default `INFO` level in
that run, so the long value shows only when something logs inside the
call.

Read, not run: each record is also handed to the log shipper
(`_ship_to_orchestrator` in `agents/observability/logging.py`), which
sends it on to the orchestrator. What the shipper's queue and the
orchestrator's log buffer do with a record of that size was not looked
at.

## Impact

Whoever can open an agent's gRPC port chooses how much memory one call
makes the agent hold, and how long the log records of that call are.
The agent's gRPC service has no authentication
([ISSUE-0153](ISSUE-0153-compose-ports-open-on-every-interface.md)
recorded that), so the port is the only gate.

Who that is today: the shipped compose file publishes no agent port to
the host, so there it is the other containers on the compose network.
An agent started without `--host` listens on 127.0.0.1 only. A
deployment that puts an agent's port on a network widens it to that
network.

It takes a hand-built client. The gRPC Python client waits for the
settings, and is held to the limit.

## Proposed fix / investigation path

Two separate things, a test first for each.

1. **Cut the four ids where they are bound.** An id is a short string. A
   value longer than a small fixed length could be dropped, or cut and
   marked as cut, in `_bind_from_metadata`. The test: a call that sends
   a long id writes no long record.
2. **See whether the server can hold the limit from the first frame.**
   gRPC has server options for the size of metadata
   (`grpc.max_metadata_size`, `grpc.absolute_max_metadata_size`).
   Whether either applies before the client has acknowledged the
   settings was not tried. If none does, the agent's own code is the
   place to refuse an oversized value, and the first item is the part
   that can be fixed here.

## Notes

> 2026-11-21 — found on the way by the review of pull request 1138 on
> 2026-11-17, which measured the server and read the logging code. Both
> halves were run again when this was filed; the tables hold that run's
> numbers. Severity proposed from the tracker's precedent
> ([ISSUE-0265](ISSUE-0265-merged-log-view-reads-every-finished-run-from-disk.md),
> low: one request makes the orchestrator read up to 512 MB of log files);
> not ruled by the maintainer.

> 2026-11-28 — severity ruled `low`, as proposed: the maintainer's
> ruling at the
> [strategy review of 2026-11-28](../project-strategy-review-2026-11-28.md#3-the-maintainers-rulings),
> ratified by ruling (ab) of the
> [sequencing Amendment 2026-11-28](../v0.4.x-sequencing-2026-11-28.md#rulings).
