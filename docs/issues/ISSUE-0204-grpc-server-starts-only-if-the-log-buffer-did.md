---
id: ISSUE-0204
summary: "The orchestrator treats a log-buffer start failure as harmless, but it builds the whole agent-facing gRPC server, wallet service included, only when the buffer started. When the buffer's folder cannot be created or listed the health check says ok, the gRPC port stays closed, and every agent model call fails for want of a wallet lease."
status: open
severity: medium
area: cmd/orchestrator
created: 2026-11-10
refs:
  - cmd/orchestrator/main.go
  - cmd/orchestrator/grpcserver.go
  - internal/observability/logbuffer/disk.go
  - agents/wallet_client.py
  - evaluators/exp001/processes.py
---

# ISSUE-0204: The agent-facing gRPC server starts only if the log buffer did

## Summary

`main()` in `cmd/orchestrator/main.go` creates the log buffer early. If
that fails it logs "failed to initialize log buffer; log endpoints
disabled" and carries on, on the stated ground that only the log endpoints
are affected. Further down, the gRPC listener and server are created
inside `if logBuf != nil`. That server carries the wallet service as well
as the log service, so a buffer failure removes both.

## Context

Run with `PERSATRIX_LOGBUFFER_DIR` pointing at a path the process cannot
create:

- The orchestrator logged "wallet lease enforcement initialized".
- `/healthz` answered ok.
- Nothing listened on the gRPC port.

The buffer fails to start only when its folder cannot be created or
cannot be listed (`newDiskStore` in
`internal/observability/logbuffer/disk.go`): a missing folder under a
read-only parent, a folder the process may not open, or a file at that
path. A folder that exists but is read-only starts fine, and so does a
full disk.

Agents refuse to call a model when the wallet cannot be reached
(`agents/wallet_client.py` fails closed), so the whole fleet goes silent
behind an orchestrator that reports itself healthy. `cmd/orchestrator` is
at 47.8% test coverage and `main()` itself is not covered, so no test
drives this path. The EXP-001 harness already works around it: a comment
in `evaluators/exp001/processes.py` says the log buffer must start "or
the orchestrator serves no wallet".

A cost config that fails to load has the same shape. `main()` logs
"failed to load cost config, budget enforcement disabled" and carries on,
no wallet service is registered, and the agents fail closed on that too.
This second path was read, not run.

## Impact

A wrong volume mount or wrong ownership on the log-buffer path, such as
the default `data/logs` under a read-only parent, stops every persona and
every workflow step, and the only clue is one warning at startup about
log endpoints.

## Proposed fix / investigation path

Start the gRPC server whenever the wallet exists, and register the log
service only when the buffer does. Or make the buffer failure fatal, so
the process does not start half-working. Treat a cost config that fails
to load the same way. Either way, have the health check report a missing
agent-facing listener or a missing wallet.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; reproduced with a scratch-built orchestrator binary.
