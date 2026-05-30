---
id: ISSUE-0083
summary: "The orchestrator session binding keys on `(agent, channel, sender)` (`internal/channels/session_binding.go`; resolved at `grpc_dispatcher.go:219` from `participantID, msg.ChannelID, msg.SenderID`), so in a group channel with two+ senders one agent gets a *separate* session per speaker — fragmenting its shared memory of one room by who talked. Per the scope-axes reframing the session unit is room-continuity `(agent, channel)`; the sender axis must be dropped. DMs are unaffected (one peer, so the triple already collapses to the pair); the fragmentation bites multi-party rooms, which become the v0.5.0 bridge mainline. Inverts the `grpc_dispatcher_session_test.go` \"two senders → distinct sessions\" assertion."
status: open
severity: medium
area: internal/channels
created: 2026-05-30
refs:
  - docs/memory-scope-axes.md
  - docs/rfcs/0031-per-session-namespacing-channels.md
  - internal/channels/session_binding.go
  - internal/channels/grpc_dispatcher.go
  - internal/channels/grpc_dispatcher_session_test.go
---

## Summary

The per-request session unit shipped by [ISSUE-0082](ISSUE-0082-orchestrator-per-request-session-principal-emission.md) keys on the triple `(agent, channel, sender)`:

- [`internal/channels/session_binding.go`](../../internal/channels/session_binding.go) — `SessionResolver.Resolve(ctx, agentID, channelID, userID)` mints + persists one `session_id` per `(agent_id, channel_id, user_id)` row, and `ErrEmptySessionAxis` requires all three non-empty.
- [`internal/channels/grpc_dispatcher.go:219`](../../internal/channels/grpc_dispatcher.go) — the live call is `d.sessions.Resolve(ctx, participantID, msg.ChannelID, msg.SenderID)`: the third axis is the message **sender**, not an authenticated user.

The [scope-axes reframing](../memory-scope-axes.md) redefines a session as **room continuity, keyed `(agent, channel)`** (see the RFC [§A amendment](../rfcs/0031-per-session-namespacing-channels.md#a-vocabulary)). The sender axis must be dropped.

## Context

The sender axis was introduced for concurrency isolation: "two peers in one channel, or two DM threads with one agent, are distinct sessions even within one process" ([ISSUE-0081](ISSUE-0081-session-id-process-global-not-task-local.md) / RFC §B amendment). But:

- **Two DM threads with one agent are already distinct channel ids** (`dm:a:b` vs `dm:c:b`), so the channel axis alone keeps them isolated — the sender axis is redundant there.
- **The only case the sender axis actually changes is co-speakers in one group room** — and that is exactly the case it gets *wrong*: agent X talking in `group:planning` with senders Alice and Bob gets two sessions, `(X, planning, Alice)` and `(X, planning, Bob)`, so its episodic memory of one conversation is split by who spoke. When Alice references something Bob said, X recalls under a session that never saw Bob's turn.

This is pinned (as the *intended* behaviour, which this issue reverses) by [`grpc_dispatcher_session_test.go`](../../internal/channels/grpc_dispatcher_session_test.go): *"two senders in one channel must resolve to distinct sessions (per-conversation isolation)."*

## Impact

- **Multi-party rooms lose shared context per agent.** Today this primarily affects internal `group:` channels (RFC 0011); it becomes load-bearing at v0.5.0 when Slack/Discord/email bridges make multi-human + multi-agent rooms the mainline. Recall filtering (RFC 0031 Phase 2) is live, so the fragmentation is now observable, not latent.
- **DMs and single-peer chat are unaffected** — one sender means `(agent, channel, sender)` already collapses to `(agent, channel)`, so there is no behaviour change on the dominant current path.
- The fragmentation does **not** affect the relationship tier (cross-room, per-participant by design) or person-subject facts (which should be person-scoped — [ISSUE-0084](ISSUE-0084-fact-scope-by-subject-not-uniform-session.md)); it is specifically the episodic/room continuity that splits.

## Proposed fix / investigation path

1. **`SessionResolver`** — drop the `user_id` axis: bind on `(agent_id, channel_id)`, migrate the `session_bindings` table's unique key + `ON CONFLICT` target from the triple to the pair. `ErrEmptySessionAxis` loses the user component (the no-sender edge case stops being special — it degrades to the same room session as any other speaker rather than to the `legacy` snapshot).
2. **`grpc_dispatcher.go:219`** — call `Resolve(ctx, participantID, msg.ChannelID)`; stop passing `msg.SenderID` into the session key (the sender stays available for the relationship/facts write paths, which *are* per-participant).
3. **Tests** — invert the `grpc_dispatcher_session_test.go` assertion: two senders in one channel resolve to **one** shared room session; add a positive multi-party-room test (two senders → same `persatrix-session` header → shared recall). Keep the concurrent-dispatch isolation test for *distinct channels* (still independent).
4. **Migration** — the `session_bindings` rebuild needs a schema-version bump on the channel store; decide whether to drop existing triple-keyed bindings (they re-mint on next message) or collapse them.

> Sequencing: this is the load-bearing prerequisite for the [scope-axes reframing](../memory-scope-axes.md). It is upstream of the RFC 0031 Phase 3 operator CLI (which is otherwise unaffected — see the [Phase 3 plan amendment](../rfcs/0031-phase3-pr-plan.md#amendment--scope-axes-reframing)).
