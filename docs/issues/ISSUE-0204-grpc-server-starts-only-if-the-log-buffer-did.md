---
id: ISSUE-0204
summary: "The orchestrator treats a log-buffer start failure as harmless, but it builds the whole agent-facing gRPC server, wallet service included, only when the buffer started. With an unwritable buffer folder the health check says ok, the gRPC port stays closed, and every agent model call fails for want of a wallet lease."
status: open
severity: medium
area: cmd/orchestrator
created: 2026-11-10
refs:
  - cmd/orchestrator/main.go
  - cmd/orchestrator/grpcserver.go
  - agents/wallet_client.py
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

Run with `PERSATRIX_LOGBUFFER_DIR` pointing at a folder the process
cannot write:

- The orchestrator logged "wallet lease enforcement initialized".
- `/healthz` answered ok.
- Nothing listened on the gRPC port.

Agents refuse to call a model when the wallet cannot be reached
(`agents/wallet_client.py` fails closed), so the whole fleet goes silent
behind an orchestrator that reports itself healthy. `cmd/orchestrator` is
at 47.8% test coverage and `main()` itself is not covered, so no test
drives this path.

## Impact

A full disk or a wrong volume mount on the log-buffer folder stops every
persona and every workflow step, and the only clue is one warning at
startup about log endpoints.

## Proposed fix / investigation path

Start the gRPC server whenever the wallet exists, and register the log
service only when the buffer does. Or make the buffer failure fatal, so
the process does not start half-working. Either way, have the health
check report a missing agent-facing listener.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; reproduced with a scratch-built orchestrator binary.
