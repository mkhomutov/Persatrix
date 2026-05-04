---
id: ISSUE-0014
summary: ChannelRouter.fanout dispatches inline per-recipient; worst-case publish latency is O(N × 5s) once the gRPC dispatcher lands
status: open
severity: low
area: internal/channels
created: 2026-05-04
refs:
  - docs/rfcs/0011-channels.md
  - docs/rfcs/0011-pr-plan.md
  - docs/pr-reviews/pr-245-review.md
---

## Summary

`ChannelRouter.fanout` in `internal/channels/router.go` dispatches
sequentially per recipient with a 5s per-recipient context timeout. With
the v0.3.0 `NoopDispatcher` this is O(N) cheap operations and fine. Once
PR 4 wires the gRPC dispatcher, worst-case publish latency becomes
O(N × 5s) — a single slow agent stalls the entire fanout tail.

## Context

Captured during PR #245 deep review (Nice-to-have #3). The detached
`context.WithoutCancel(ctx)` + per-recipient timeout pattern is the right
foundation; only the iteration shape needs to change.

## Impact

- v0.3.0: none (NoopDispatcher).
- PR 4 onward: degraded p99 publish latency on channels with >5 members
  if any recipient is slow or hung.

## Proposed fix / investigation path

Replace the inline loop with a bounded-concurrency `errgroup` (or a small
fixed worker pool) sized via constant or config:

```go
g, gctx := errgroup.WithContext(detached)
g.SetLimit(channelFanoutMaxConcurrency) // e.g. 16
for _, p := range eligible {
    p := p
    g.Go(func() error {
        rctx, cancel := context.WithTimeout(gctx, channelFanoutPerRecipientTimeout)
        defer cancel()
        return r.dispatcher.Dispatch(rctx, p, msg)
    })
}
_ = g.Wait() // current behaviour: errors are tolerated, only metric is incremented
```

Pin this work to PR 4's plan so it lands together with the gRPC
dispatcher that makes it necessary.

## Notes

> 2026-05-04 — initial capture during PR #245 review (Nice-to-have #3).
