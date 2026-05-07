---
id: ISSUE-0028
summary: "selectChannelDispatcher extracted with 'independently testable' rationale but has no unit test"
status: resolved
severity: low
area: cmd/orchestrator
created: 2026-05-05
closed: 2026-05-07
closed_pr:
refs:
  - cmd/orchestrator/channels.go
  - cmd/orchestrator/channels_test.go
---

## Summary

`selectChannelDispatcher(reg registry.Registry, log *zap.Logger)` was
extracted from `initChannels` in PR #250 with a comment citing
"independently testable" as the motivation. No test in
`cmd/orchestrator/channels_test.go` exercises it directly.

## Context

Observed during PR #250 review (Should-Fix #2). The function is small
(nil-check → return `NoopDispatcher{}` else `*GRPCMessageDispatcher`)
but the extraction-rationale-without-test pattern is precisely what
this repository's `docs/issues/` lifecycle is designed to surface.

## Impact

- The extracted helper carries an aspirational doc-comment that no
  test honours, eroding trust in similar comments elsewhere.
- A future change that flips the nil-vs-non-nil branch (e.g. swapping
  in a different no-op implementation) will not be caught.

## Proposed fix / investigation path

Either:

1. Add a two-row table-driven test asserting the returned concrete
   type for `nil` and a non-nil `registry.Registry` fake.
2. Inline `selectChannelDispatcher` back into `initChannels` and
   remove the testability rationale.

Option 1 is preferred (≈10 lines including imports).

## Notes

> 2026-05-05 — initial capture during PR #250 review (Should-Fix #2).
>
> 2026-05-07 — already-resolved sweep. The two table-driven tests
> proposed by Option 1 (`TestSelectChannelDispatcher_NilRegistryReturnsNoop`
> and `TestSelectChannelDispatcher_NonNilRegistryReturnsGRPC`) were
> actually added inside PR #250 itself
> ([cmd/orchestrator/channels_test.go:93-117](../../cmd/orchestrator/channels_test.go#L93-L117)),
> so the issue file shipped stale on day one. Closing as resolved with
> no further code change required; the nil-registry test is extended in
> the same sweep to also cover ISSUE-0031.
