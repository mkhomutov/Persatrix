---
id: ISSUE-0035
summary: "Remove dead-but-wired chatExecutor / WithChatExecutor / SendChatMessage proto entry once the v0.3.0 upgrade window closes"
status: open
severity: low
area: cmd/orchestrator
created: 2026-05-05
refs:
  - cmd/orchestrator/main.go
  - internal/executor/chat_executor.go
  - internal/server/options.go
  - internal/server/chat_handler.go
  - agents/server_servicers.py
  - proto/orchestrator.proto
  - docs/rfcs/0011-amendment-chat-as-dm.md
---

## Summary

After PR #251 (RFC 0011 PR 4a-ii-β-2), the chat REST handler routes
through the channels DM publish-and-await path and no longer reads
`s.chatExecutor` at runtime. The construction in
[`cmd/orchestrator/main.go:310-314`](../../cmd/orchestrator/main.go#L310-L314)
(`executor.NewGRPCChatExecutor` + `server.WithChatExecutor(chatExec)`)
is preserved for one release window as binary-upgrade compat, with an
inline `TODO(post-PR-251)` documenting the removal plan. The
Python-side `agents/server_servicers.py::SendChatMessage` is similarly
orphaned but kept as a regression guard. This issue tracks the cleanup
so the removal task is visible in `docs/issues/INDEX.md` rather than
relying on a TODO grep.

## Context

Captured during the PR #251 deep review (Finding L-1). The author is
already aware — the inline TODO at
[`cmd/orchestrator/main.go:299-310`](../../cmd/orchestrator/main.go#L299-L310)
spells out the removal scope. No tracked issue existed before this one,
so the cleanup risked falling off the radar between v0.3.0 and v0.4.0.

The dead-but-wired surfaces are:

| File / symbol | Status |
|---------------|--------|
| `cmd/orchestrator/main.go` — `chatExec := executor.NewGRPCChatExecutor(...)` | constructed, never used by handler |
| `cmd/orchestrator/main.go` — `srvOpts = append(srvOpts, server.WithChatExecutor(chatExec))` | option set, never read |
| `internal/executor/chat_executor.go` — `GRPCChatExecutor` | dead production path |
| `internal/server/options.go` — `WithChatExecutor` | sets `s.chatExecutor`, never consumed |
| `internal/server/chat_handler.go` — `s.chatExecutor` field | unread after rewrite |
| `agents/server_servicers.py` — `SendChatMessage` RPC | orphaned servicer; clients no longer call it |
| `proto/orchestrator.proto` — `SendChatMessage` rpc | orphaned wire entry |

## Impact

- **Code-rot risk**: dead code accumulates dependencies (otelgrpc dial
  options, registry plumbing) that future contributors mistakenly
  treat as load-bearing.
- **Wire-surface confusion**: clients reading the proto see a
  `SendChatMessage` RPC that succeeds at the wire level but is
  semantically deprecated. Without an explicit deprecation marker
  (`option deprecated = true;`) and an accompanying issue,
  third-party tooling will keep generating bindings for it.
- **Test cost**: every package that runs against the orchestrator
  binary still pays the construction cost (`grpc.NewClient`,
  registry handle, dial-options stitching) on every test setup.

## Proposed fix / investigation path

When the v0.3.0 upgrade window closes (target: v0.4.0 milestone), drop
the symbols above in a single PR:

1. **Go-side**: delete `executor.GRPCChatExecutor`, `executor.NewGRPCChatExecutor`,
   `server.WithChatExecutor`, the `chatExecutor` field on `Server`, and
   the construction block + TODO in `cmd/orchestrator/main.go`. Remove
   any `WithChatExecutor(...)` calls in test fixtures.
2. **Python-side**: delete `SendChatMessage` from
   `agents/server_servicers.py`. Verify no agent test still calls the
   stub.
3. **Proto-side**: delete the `SendChatMessage` rpc from the `.proto`
   file, regenerate stubs (`make proto`), and confirm no Go or Python
   code still imports the generated symbol.
4. **Smoke test**: run the chat-as-DM test suites
   (`internal/server/chat_handler_test.go`,
   `internal/server/chat_handler_review_test.go`) and the integration
   suite to confirm no regression.

The cleanup PR's commit message should reference both PR #251 (which
introduced the dead-but-wired state) and this issue.

## Notes

> 2026-05-05 — captured during PR #251 deep review (Finding L-1). The
> inline TODO already exists; this issue is the tracker so the work
> shows up in [INDEX.md](INDEX.md) and not just `grep -rn TODO`.
