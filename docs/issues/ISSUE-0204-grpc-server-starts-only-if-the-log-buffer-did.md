---
id: ISSUE-0204
summary: "The orchestrator treats a log-buffer start failure as harmless, but it builds the whole agent-facing gRPC server, wallet service included, only when the buffer started. When the buffer's folder cannot be created or listed the health check says ok, the gRPC port stays closed, and every agent model call fails for want of a wallet lease."
status: resolved
severity: medium
area: cmd/orchestrator
created: 2026-11-10
closed: 2026-11-16
closed_pr: 1135
refs:
  - cmd/orchestrator/main.go
  - cmd/orchestrator/grpcserver.go
  - cmd/orchestrator/cost.go
  - cmd/orchestrator/process_test.go
  - internal/server/server.go
  - internal/observability/logbuffer/disk.go
  - agents/wallet_client.py
  - agents/observability/log_shipper.py
  - tests/unit/python/test_log_shipper_without_log_service.py
  - evaluators/exp001/processes.py
  - docs/issues/ISSUE-0273-log-shipper-treats-a-missing-log-service-as-an-outage.md
  - docs/issues/ISSUE-0274-exp001-harness-comment-says-no-wallet-without-the-log-buffer.md
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

> 2026-11-13 — the fix for
> [ISSUE-0211](ISSUE-0211-misspelled-budget-key-removes-every-spending-cap.md)
> makes the cost-config path above easier to reach: a key the `cost`
> block does not have, and a file with no `cost.budgets`, are now load
> errors, where they used to load as "no limit". Such a file leaves the
> orchestrator running with no wallet service. That is the safe side of
> the old behaviour, and it is still this issue's half-working state.
> This issue itself is unchanged.

> 2026-11-16 — resolved by [#1135](https://github.com/mkhomutov/Persatrix/pull/1135), on the maintainer's three
> choices.
>
> **The log buffer cannot start: the orchestrator keeps going without
> logs.** The agent-facing gRPC port opens whether or not the buffer
> started (`listenForAgents` in `cmd/orchestrator/grpcserver.go`), with
> the wallet service on it; only the log service is left out. The
> start-up warning now reads `the log buffer did not start, so the
> orchestrator runs without it: the log routes answer 501 and logs that
> agents ship are refused; agents are served all the same, the wallet
> included`.
>
> **The cost config fails to load: the orchestrator refuses to start.**
> It logs `refusing to start: the cost config did not load, and without
> it there is no wallet, so no agent could call a model` at `FATAL`, with
> the file and the loader's reason in the `error` field, and exits with
> status 1. The refusal comes before the audit log, the stores and the
> two ports are opened, so it leaves no audit log, no store and no port
> behind. The log buffer, tracing and metrics start before it: the log
> folder is made, and stays empty. "Fails to load" covers a
> config folder with no `optimization.yaml`, a file that cannot be read,
> one that is not YAML, a `cost` block with a key the loader does not
> know or with no `budgets`, and a limit or an `on_exceed` value the
> loader refuses. Every shipped start has a file that loads: the root
> `config/` and the seven folders under `config/demo/`, which the compose
> files and the Makefile's demo targets mount, are each loaded by a test,
> and the EXP-001 harness writes its own with the `budgets` block kept.
> The orchestrator image holds no config of its own, so a container
> started with no config folder mounted now exits where it used to run
> with no wallet.
>
> **The health check says what is missing and still answers 200.**
> `GET /healthz` answers `{"status":"ok","missing":["log_buffer"]}` when
> the buffer did not start, and `{"status":"ok"}`, byte for byte as
> before, when nothing is missing. Every reader in the repository reads
> the status code only: the compose healthcheck, `make demo-autonomous`,
> the two integration tests that start an orchestrator, the manual-test
> scripts and the EXP-001 harness. The CLI and the web console do not
> call the route.
>
> What an agent does with no log service was read and then run. A record
> written to the stream that the orchestrator has already ended is
> accepted, and the failure shows only when the next record is written.
> The shipper then logs one warning to the agent's own output, waits (1
> second, doubling to 30, never reset by a failed try) and opens a new
> stream. So an agent that keeps logging writes six warnings in the first
> half minute and then one each 30 seconds, and a quiet agent writes
> none. No turn waits on it: a log call only appends to a queue of at
> most 5,000 records. The records are lost to the orchestrator and stay
> in the agent's own output.
> [ISSUE-0273](ISSUE-0273-log-shipper-treats-a-missing-log-service-as-an-outage.md)
> records what that leaves to improve.
>
> The comment in `evaluators/exp001/processes.py` quoted above is no
> longer true. That folder is not changed during the experiment's scoring
> period;
> [ISSUE-0274](ISSUE-0274-exp001-harness-comment-says-no-wallet-without-the-log-buffer.md)
> holds the correction.
