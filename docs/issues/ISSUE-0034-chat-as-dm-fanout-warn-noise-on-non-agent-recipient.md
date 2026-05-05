---
id: ISSUE-0034
summary: "chat-as-DM reply fanout fires per-reply WARN because the user (DM member) is not in the agent registry"
status: open
severity: medium
area: internal/channels
created: 2026-05-05
refs:
  - internal/channels/grpc_dispatcher.go
  - internal/channels/router.go
  - internal/channels/sqlite_query.go
  - internal/server/chat_handler.go
  - docs/rfcs/0011-amendment-chat-as-dm.md
---

## Summary

Every chat reply emitted by an agent (`agent → user`) produces a
structured `level=warn` log line in the orchestrator because
`GRPCMessageDispatcher.Dispatch` cannot resolve the user (a DM
member) in the agent registry and falls through its
`ErrAgentNotFound` arm at
[`internal/channels/grpc_dispatcher.go:99-114`](../../internal/channels/grpc_dispatcher.go#L99-L114).
At even modest chat QPS this floods log scrapes — the same anti-pattern
the PR-245 round-3 `Once`-gating fix was meant to avoid for the chat
fallback warning.

## Context

Captured during the PR #251 deep review. The interaction is structural,
not a bug in the new waiter logic:

1. `GetOrCreateDM` at
   [`internal/channels/sqlite_query.go:200-213`](../../internal/channels/sqlite_query.go#L200-L213)
   adds **both** participants (`agent-x`, `alice`) with
   `respond_policy = 'always'`.
2. The chat handler calls `PublishAndAwait`; the agent replies via the
   REST `/api/v1/channels/{id}/messages` path.
3. `ChannelRouter.Publish` runs the standard fanout: `r.fanout` resolves
   DM members, filters the sender (`agent-x`), and dispatches to the
   remaining member (`alice`).
4. `GRPCMessageDispatcher.Dispatch("alice", msg)` calls
   `resolver.Get(ctx, "alice")` — `alice` is not a registered agent, so
   the resolver returns `registry.ErrAgentNotFound` and the dispatcher
   logs the WARN ("channels: dispatch target not registered; dropping
   (read via history on reconnect)").

The chat caller already has the reply via the in-process waiter
([`internal/channels/waiter.go`](../../internal/channels/waiter.go)),
so the fanout to the user is structurally redundant for the chat-as-DM
path — it exists to satisfy the "every publish fans out to every
non-sender member" invariant from the pre-chat channels model.

## Impact

- **Log-volume budget regression**: every chat turn produces one
  guaranteed WARN per reply. Operators will reach for grep filters; any
  alert rule that fires on `level=warn AND component=channels` will
  go off on healthy traffic.
- **Alarm-fatigue precedent**: the PR-245 round-3 review explicitly
  flagged the per-request WARN pattern as something to gate behind a
  `sync.Once` (now ISSUE-0009 / PR #246). This finding reintroduces the
  same shape (per-event WARN on a structurally-expected configuration)
  through a different code path — the fix should respect that prior
  decision.
- **Diagnostic dilution**: the WARN line is genuinely useful when an
  operator misconfigures a `channels.yaml` membership against an
  unregistered agent ID. Drowning it in chat traffic erodes the
  signal:noise ratio of the original WARN.

## Proposed fix / investigation path

Three viable strategies, listed cheapest → most invasive. Recommendation
is option 2.

1. **Demote the dispatcher WARN to DEBUG when the recipient is on a
   `dm:` channel.** Tightest scope; one-line change in
   `grpc_dispatcher.go`. Loses the genuinely-useful WARN for `dm:`
   channels with mistyped agent IDs (mitigated: `dm:` channels are
   created server-side by `GetOrCreateDM`, never from `channels.yaml`,
   so the misconfiguration class does not apply).

2. **Add the user as a DM member with `respond_policy = 'never'`** and
   the agent with `'always'` in `GetOrCreateDM`. The router's
   `RespondNever` short-circuit at
   [`internal/channels/router.go:286-293`](../../internal/channels/router.go#L286-L293)
   then skips the dispatch entirely — no resolver lookup, no WARN, no
   wasted gRPC dial. Closer to the semantic truth: users do not "respond"
   via gRPC push, they read via history. Requires a small migration on
   existing `dm:` rows but `RespondNever` is already a tested code path.

3. **Skip fanout entirely for DM channels** in the chat-as-DM publish
   path. Riskiest because it inverts the fanout invariant; would need
   to be opt-in per channel type rather than a blanket skip.

Whichever option lands, the fix must include a regression test asserting
that a chat reply does **not** emit the
`channels: dispatch target not registered` WARN — none of the existing
tests pin this. A `zap/zaptest/observer` filter for that exact message
snippet, asserting `Len == 0` after a successful chat round-trip, is
the right shape (mirrors `TestHandleChat_LocalFallback_WarnsOncePerProcess`
in
[`internal/server/chat_handler_review_test.go`](../../internal/server/chat_handler_review_test.go)).

## Notes

> 2026-05-05 — captured during PR #251 deep review (Finding M-1). Not a
> regression introduced by the waiter logic; falls out of the
> chat-as-DM model where users are first-class DM participants but
> never agent-registry entries. Worth fixing on its own merits before
> the WARN noise becomes load-bearing on log-volume budgets.
