---
id: ISSUE-0176
summary: "Every orchestrator stop waited out the whole 12-second shutdown drain and logged \"shutdown drain timed out, exiting\", even with nothing in flight: main() waited for the agent-facing gRPC server to return before it stopped that server. With an agent still connected, whose log shipper holds a stream open, the orchestrator did not exit at all until killed, and its deferred closes never ran. Docker's default 10-second stop timeout, which the compose orchestrator service keeps, is shorter than the drain. EXP-001 paid the 12 seconds at every deployment it stopped."
status: resolved
severity: medium
area: cmd/orchestrator
created: 2026-09-28
closed: 2026-09-28
closed_pr: 1017
refs:
  - cmd/orchestrator/main.go
  - cmd/orchestrator/grpcserver.go
  - internal/server/server.go
  - evaluators/exp001/processes.py
  - docker-compose.yaml
---

# ISSUE-0176: Every orchestrator stop waits out the whole shutdown drain

## Summary

On SIGTERM or SIGINT the orchestrator cancels its root context and waits up
to `shutdownDrainTimeout` (12 seconds) for its background work to finish.
One piece is the agent-facing gRPC server, which serves LogService and
WalletService, and its goroutine returns only once that server is stopped.
`main()` stopped it with a deferred `GracefulStop`, which runs only after
the drain it was holding up. So every stop waited the full 12 seconds and
logged `shutdown drain timed out, exiting`, even when nothing was in flight.

## Context

Seen in the EXP-001 harness's real-process tests: every orchestrator stop
took 12.0 seconds. The harness stops the advisers first, then the
orchestrator, so no agent was connected by then.

Reproduced with the orchestrator binary started with the harness's flags,
then sent SIGTERM:

| Case | Before | After |
|---|---|---|
| Nothing connected (the harness's order) | 12.07 s, "drain timed out" | 0.02 s, "drained cleanly" |
| One agent log stream held open | did not exit; killed after 40 s | 5.04 s, "drained cleanly" |

## Impact

- Each stop took 12 seconds longer than it needed to, and logged a warning
  that suggests something was stuck. EXP-001 paid this at every deployment
  it stopped.
- While an agent was still connected, the orchestrator never exited. An
  agent's log shipper keeps a LogService stream open for as long as the
  agent runs, and the deferred `GracefulStop` waited for it. Being the last
  deferred call registered, it runs first, so none of the others ran:
  nothing closed the account and channel stores, the audit log or the log
  buffer, and nothing flushed the telemetry. Only a kill ended the process.
- The compose orchestrator service sets no `stop_grace_period`, so Docker
  kills it 10 seconds after asking it to stop: before the 12-second drain
  ends, whether or not agents are connected. Its deferred closes never ran
  there either.

## Fix

`serveAgentGRPC` in `cmd/orchestrator/grpcserver.go` runs the gRPC server
the way `server.Start` runs the HTTP one: it serves until the root context
is cancelled, then stops the server and returns, so the drain can finish.
The stop is graceful first, so a call in flight, such as a wallet lease
call, finishes. After `grpcStopGrace` (5 seconds) it stops hard, which ends
the log streams still open; the shippers reconnect. The deferred call is now
`Stop`, which never waits on an open stream. It is a backstop for when
`Serve` fails on its own.

## Notes

- A log tail on the HTTP API (`GET /api/v1/executions/{id}/logs/stream`)
  still holds the HTTP server's own drain for its whole 10-second window.
  `http.Server.Shutdown` waits for a response in progress, and the stream
  ends only when its client leaves or the log buffer closes, which happens
  after the drain. Measured: 10.05 s with one tail attached. That is bounded
  and inside the drain, so it is not this issue, but it is as long as
  Docker's default stop timeout.

> 2026-09-28 — found while reviewing EXP-001 harness PR 5c
> ([#1015](https://github.com/mkhomutov/Persatrix/pull/1015)).
