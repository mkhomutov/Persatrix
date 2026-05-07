---
id: ISSUE-0003
summary: "RateLimiter.Allow allocates a transient slice in evictOlderThan on every admit; compact in place"
status: resolved
severity: medium
area: security
created: 2026-05-04
closed: 2026-05-07
refs:
  - docs/rfcs/0009-security-sandboxing.md
---

## Summary

`(*agentRing).evictOlderThan` allocates `kept := make([]time.Time, 0, r.count)`
on every `Allow()` invocation, then copies the survivors back into the ring's
fixed backing array. The ring already owns that backing array — the auxiliary
slice is unnecessary.

## Context

- File: [internal/security/ratelimit.go](../../internal/security/ratelimit.go) → `(*agentRing).evictOlderThan`.
- Default config (60 calls/window, ~1k tracked agents) is fine; with
  `CallsPerWindow=600` under sustained traffic the per-admit allocation
  becomes measurable GC pressure — exactly the load profile a flooding
  attacker would create.

## Impact

Performance under attack. Not a correctness defect.

## Proposed fix / investigation path

1. In-place compaction: walk the ring forward from `head`, rewrite surviving
   timestamps to a moving write index, update `head`/`count` at the end. No
   allocations.
2. Alternative: skip eviction on admit entirely. The ring size already caps
   `count`; expired-but-still-counted entries are bounded by `CallsPerWindow`,
   so the worst-case overcount is one window's worth of stale timestamps.
3. Add a `BenchmarkAllowSteadyState` next to `TestSlidingWindow_ConcurrentSafe`
   to lock in the regression guard.

## Notes

> 2026-05-04 — captured during PR #244 deep review (R3, finding M-R3-01).
>
> 2026-05-07 — resolved via in-place count-shrink. Timestamps in the
> ring are appended in chronological order (Now is monotonic in both
> production and `fakeClock`), and cutoff is monotonic too, so expired
> entries always form a contiguous prefix at the chronological start
> of the ring. evictOlderThan now counts the expired prefix and shrinks
> `r.count` by that amount; `r.head` is unchanged because new admits
> still land at the same physical slot, and the logical start
> `(head - count + cap)` rebases automatically. No memory move, no
> allocation. Benchmark on `CallsPerWindow=600`: 5979 ns/op → 67.8 ns/op
> (~88×), 16372 B/op → 0 B/op, 1 alloc/op → 0 allocs/op. Regression
> guard: `TestRateLimiter_AllowSteadyStateZeroAlloc` (uses
> `testing.AllocsPerRun`) plus `BenchmarkAllowSteadyState`.
