---
id: ISSUE-0245
summary: "The orchestrator's audit log is one file that only grows: nothing rotates it, caps it or archives it. Several of its events are written once per request (a task sent to an agent, a channel recall, a call that names a new agent id once the limiter's table is full, a refused operator route), so its size follows traffic for as long as the deployment lives."
status: open
severity: low
area: security
created: 2026-11-13
refs:
  - internal/security/audit.go
  - internal/security/audit_chain.go
  - cmd/orchestrator/audit.go
  - docs/observability.md
  - docs/issues/ISSUE-0208-every-anonymous-rest-call-writes-a-synced-audit-line.md
---

# ISSUE-0245: The audit log has no rotation and no size bound

## Summary

The audit log is a single append-only file, `data/logs/audit.jsonl` by
default (`OBSERVABILITY_AUDIT_PATH`). `NewFileAuditLogger`
(`internal/security/audit.go`) opens it for append and never starts a
new one. No setting bounds its size or its age.

## Context

- An agent's decision log does rotate: `docs/observability.md`
  documents `max_bytes` and `keep` for it. The audit log has no
  counterpart.
- Each line carries a checksum chained to the line before it, and
  `VerifyChain` (`internal/security/audit_chain.go`) reads the file from
  its first line. A rotation therefore has to say how a chain continues
  across files, which is why this is not a one-line change. A comment in
  `audit_chain.go` already notes that an external log rotator would
  break the single-writer assumption the recovery code relies on.
- Start-up reads only the last line (`inspectTail`), so a large file
  does not slow a restart. `VerifyChain` does read it all.
- [ISSUE-0208](ISSUE-0208-every-anonymous-rest-call-writes-a-synced-audit-line.md)
  removed the largest single source of lines: one per REST call from a
  caller with no agent id. The events still written once per request:
  - `tool.invoked`, one per task sent to an agent
    (`internal/executor/dispatch.go`), and `channel.recall`
    (`internal/server/persona_recall_handlers.go`). Both are
    telemetry-class, so batched.
  - `rate_limit.agent_evicted` (`internal/security/ratelimit.go`): once
    the limiter tracks its 1,000 agent ids, every call that names a new
    one evicts another and writes a line. The caller is never refused,
    because each new id starts a fresh budget. Telemetry-class. Under
    the default `auth.mode: disabled` the id is whatever the caller
    sends.
  - `authz.denied`, one synced line per refused request, with no limiter
    in front of it:
    [ISSUE-0248](ISSUE-0248-a-refused-operator-route-writes-a-synced-audit-line-per-request.md).
  - Registration's `agent.token_issued`, `agent.registered` and
    `agent.token_invalid` are also one per request, but a limiter of
    their own caps them at 120 a minute per source address
    (`internal/server/agent_enrollment.go`).

Read from the code. No long-running deployment's file was measured.

## Impact

Slow and unbounded. A busy deployment's audit file grows until someone
truncates it by hand, which breaks the chain, or the volume fills. A
full volume stops every writer on it: the audit log shares its folder
with the run logs by default.

Proposed low: nothing is lost today, and the growth is linear in real
work. The severity is proposed here, not ruled.

## Proposed fix / investigation path

Decide the chain rule first: for instance, each new file's first line
is a `chain.rotated` event that carries the last checksum of the file
before it, and `VerifyChain` follows that link. Then rotate by size
inside the logger, which is the single writer, with a `keep` count like
the decision log's. An external rotator is the wrong tool here.

## Notes

> 2026-11-13 — noticed while fixing
> [ISSUE-0208](ISSUE-0208-every-anonymous-rest-call-writes-a-synced-audit-line.md),
> whose text says "the audit log has no rotation" and which nothing
> tracked.

> 2026-11-14 — the list of events written per request was two short:
> `rate_limit.agent_evicted` and `authz.denied`. Found in review of the
> ISSUE-0208 fix; the second is filed as ISSUE-0248.
