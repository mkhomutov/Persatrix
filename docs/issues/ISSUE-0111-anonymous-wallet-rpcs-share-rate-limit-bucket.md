---
id: ISSUE-0111
summary: "Agent wallet RPCs carried no x-agent-id metadata, so the RFC 0009 'per-agent' rate limiter degraded to ONE shared anonymous 60-calls/60s bucket for the whole fleet — the RFC 0052 bounded-close summary fan burst through it and starved a persona's close-summary lease into the '[interaction summary unavailable]' placeholder (a §D artifact-contract violation), with a sibling's denied SettleLease TTL-reaped at the granted estimate (accounting drift)"
status: in_progress
severity: medium
area: channels
created: 2026-07-21
refs:
  - docs/rfcs/0052-autonomous-agent-channels.md
  - docs/rfcs/0009-agent-security-model.md
  - docs/manual-tests/v0.3.11-execution-report.md
  - internal/security/middleware.go
  - agents/wallet_client.py
---

## Summary

The Python agents' wallet RPCs (`AcquireLease` / `SettleLease` /
`ReleaseLease`) attached **no gRPC metadata**, while the orchestrator's
RFC 0009 rate limiter keys its per-agent budget on the `x-agent-id`
metadata (`internal/security/middleware.go`, `AgentIDMetadataKey`). Every
wallet call therefore resolved to the **single shared anonymous bucket**
(default 60 calls / 60 s), turning the per-agent limiter into a
fleet-wide ceiling on the wallet path.

## Context

Surfaced live on the v0.3.11 release-prep offline smoke
(`make demo-autonomous`, 2026-07-21 — the first machine-paced,
full-fleet autonomous arc through the shipped rate-limiter defaults):
at the RFC 0052 bounded close, all N personas author their metered RFC
0020 close summaries near-simultaneously. The close fan's wallet burst,
on top of the closing round's traffic, exhausted the shared anonymous
window. `iron-fox`'s summary `AcquireLease` was denied
`RESOURCE_EXHAUSTED: rate limit exceeded` — the agent-side retry (3
attempts within ~200 ms) cannot outlast a 60 s window — so its summary
degraded to the `[interaction summary unavailable]` placeholder,
violating the §D "always produce an artifact" contract the `1 + N`
synthesis reserve exists to fund. `ember-owl`'s summary succeeded but
its `SettleLease` was eaten by the same window: the lease was TTL-reaped
65 s later and settled **at the granted estimate** instead of actuals
(spend-accounting drift). The agent-side warning also mislabelled the
denial as "the active-lease cap", which misdirects diagnosis.

CI never caught this because the deterministic suites drive the wallet
in-process (no gRPC interceptor in the loop), and prior live/demo runs
were human-paced or ended in all-silent rounds (no productive close fan
before ISSUE-0110 was fixed).

## Impact

Medium: on any autonomous bounded close the §D summary guarantee
degrades for late-leasing personas — worse as N grows (the four-vendor
roster is N=4) — and denied settles silently over-account spend at the
granted estimate. Safety is unaffected (the cap/bounds still hold;
fail-closed stays closed).

## Fix

Landed with the v0.3.11 release-prep MT-execution PR (TDD,
`agents/tests/test_wallet_client_identity.py`):

- `WalletClient` now carries its hosting agent's identity
  (`agent_id` ctor/`from_channel` kwarg; `agents/server.py` passes the
  hosted agent's id) and stamps `x-agent-id` metadata on all three
  wallet RPCs, restoring genuinely per-agent rate-limit budgets.
- The `RESOURCE_EXHAUSTED` retry warning now logs the server detail
  instead of asserting "active-lease cap".

## Residuals (tracked here, not fixed)

- The acquire retry policy (3 attempts, ~0.1 s base full-jitter
  backoff) still cannot outlast a genuinely exhausted 60 s window; the
  interceptor's `retry-after-seconds` header is not consumed. Honouring
  it on the close-summary path would make §D robust against any future
  limiter pressure.
- Other agent→orchestrator surfaces (REST publish path, log shipper)
  still ride the anonymous bucket; they degrade gracefully today but
  share the same miskeying.

## Notes

> 2026-07-21 — found + fixed during the v0.3.11 release-prep offline
> smoke; see the [v0.3.11 execution report](../manual-tests/v0.3.11-execution-report.md)
> finding F-1. Set `closed_pr` when the MT-execution PR merges.
