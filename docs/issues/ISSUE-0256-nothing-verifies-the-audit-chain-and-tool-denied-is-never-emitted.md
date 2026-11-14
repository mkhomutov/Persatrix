---
id: ISSUE-0256
summary: "The audit log chains each line to the one before so that a changed line can be detected, but nothing outside the tests ever checks the chain: no command, no start-up check, no scheduled job. And the `tool.denied` audit event is defined and never emitted, so a refused tool permission leaves no audit line and can never trip the quarantine threshold set for it."
status: open
severity: low
area: security
created: 2026-11-14
refs:
  - https://github.com/mkhomutov/Persatrix/pull/1119
  - internal/security/audit_chain.go
  - internal/security/audit_event.go
  - internal/security/security.go
  - internal/security/middleware.go
  - cmd/orchestrator/ratelimit.go
  - agents/tools/permissions.py
  - agents/tools/tool_list.py
  - docs/rfcs/0009-security-sandboxing.md
  - docs/issues/ISSUE-0245-the-audit-log-has-no-rotation-and-no-size-bound.md
  - docs/issues/ISSUE-0227-canonical-specs-describe-unbuilt-features-as-built.md
---

# ISSUE-0256: Nothing verifies the audit chain, and `tool.denied` is never emitted

## Summary

Two parts of the audit trail exist in the code and are not connected to
anything.

1. **The checksum chain is never checked.** Each audit line carries a
   checksum of the line before it. `VerifyChain` recomputes the chain and
   reports the first break. Only tests call it.
2. **`tool.denied` is never written.** The event type is defined, and the
   circuit breaker has a threshold for it. No code emits the event or
   reports the violation.

The design spec says every tool call is audited and every denial raises
an alert (§6.6). The fix for
[ISSUE-0227](ISSUE-0227-canonical-specs-describe-unbuilt-features-as-built.md)
says in the spec that neither is built. That pull request's description
put both with RFC 0009 Phases 3–4 and
[ISSUE-0245](ISSUE-0245-the-audit-log-has-no-rotation-and-no-size-bound.md).
Neither tracks them: the plan does not mention `tool.denied`, and
ISSUE-0245 is about the file's size.

## Context

Found in the review of #1119.

**The chain.** `VerifyChain` is in `internal/security/audit_chain.go`; its
own comment says "Only tests call it today", and the package comment says
neither the orchestrator nor the CLI checks the chain. RFC 0009 names a
future `persatrix audit verify` command; the CLI has none. At start-up
the logger looks at the last line only, to continue the chain. So an edit
anywhere above the last line is detectable and never detected.

One more thing a verify command has to settle: after a damaged tail the
logger writes `chain.recovered` and starts a new chain, and `VerifyChain`
stops at that line. A file that has been through one recovery cannot
verify clean as a whole.

**The event.** `AuditToolDenied` is defined in
`internal/security/audit_event.go`, in the class of events flushed to
disk one by one. `cmd/orchestrator/ratelimit.go` sets a quarantine
threshold for `ViolationToolDenied`: three in five minutes. The only
violation ever reported is a rate-limit one
(`internal/security/middleware.go`).

The permission checks run in the Python agents
(`agents/tools/permissions.py`). A refusal there writes a debug line, or a
warning for a file path, to the agent's own log. It does not reach the
orchestrator. A refused argument does a little better: it is sent as a
structured log record, which reaches the orchestrator's log stream but
not the checksummed chain (`agents/tools/tool_list.py`).

## Impact

Proposed low. Nothing is broken that worked; two protections the names in
the code suggest are not there:

- An operator who trusts the audit log because it is "tamper-evident" has
  no way to ask whether it was tampered with.
- An agent that probes its permissions, one refused call after another,
  leaves no audit line and is never quarantined for it.

The severity is proposed here, not ruled.

## Proposed fix / investigation path

1. A `persatrix audit verify` command that calls `VerifyChain`, as RFC
   0009 planned, with a stated answer for a file that holds a
   `chain.recovered` line. The Rust CLI is a thin REST client, so this
   needs either a REST route or an orchestrator subcommand.
2. Decide how a refusal in an agent reaches the orchestrator's audit
   chain. Today's path for `tool.arg_invalid` stops at the log stream.
   Until an agent's identity is proven on that path, a caller could
   forge denials to get another agent quarantined
   ([ISSUE-0152](ISSUE-0152-one-quarantine-refuses-every-anonymous-caller.md)),
   so the event and the quarantine threshold may need to land apart.
3. Until then, remove or mark the unused threshold so it does not read
   as a working control.

## Notes

> 2026-11-14 — filed from the review of #1119. The orchestration spec's
> note at §6.6 links here.
