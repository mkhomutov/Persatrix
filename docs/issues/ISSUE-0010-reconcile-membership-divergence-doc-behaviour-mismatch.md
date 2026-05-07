---
id: ISSUE-0010
summary: membershipDivergence doc claims policy drift "is logged" but function only compares id sets
status: resolved
severity: low
area: internal/channels
created: 2026-05-04
closed: 2026-05-07
closed_pr:
refs:
  - docs/rfcs/0011-channels.md
  - docs/rfcs/0011-pr-plan.md
---

## Summary

`membershipDivergence` in `internal/channels/router.go` carries a doc
comment that says divergent `respond_policy` "is logged but not considered
hard divergence in v0.3.0; OQ-deferred to PR 7". The function actually
performs an id-set-only comparison and never logs policy drift. Doc and
behaviour disagree.

## Context

Captured during PR #245 deep review (Should-Fix #2). RFC 0011 §B promises
loud-fail reconciliation on divergence; in v0.3.0 the implementation
intentionally narrows that to id-set divergence and OQ-defers
respond-policy drift to PR 7.

## Impact

- Reviewer confusion: the comment over-promises versus the code.
- Operator confusion: a config that flips a participant's `respond_policy`
  against an existing store row is silently accepted with no log line —
  matching neither the comment nor the §B prose.

## Proposed fix / investigation path

Pick one (cheap → expensive):

1. Tighten the comment to "id-set divergence only; respond-policy drift
   OQ-deferred to PR 7" and drop the "is logged" clause. Preferred for
   v0.3.0 — keeps the PR 7 OQ as the single source of truth.
2. Extend `membershipDivergence` to also detect policy drift and emit a
   structured WARN (without failing reconcile) — partial implementation of
   the PR 7 work.
3. Promote policy drift to hard divergence in PR 7 per RFC §B.

If (1) is chosen, also tighten the §B prose in the RFC to "id-set
divergence" so the spec and code agree until PR 7 lands.

## Notes

> 2026-05-04 — initial capture during PR #245 review (Should-Fix #2).
>
> 2026-05-07 — resolved with option (1): the `membershipDivergence`
> godoc was tightened to "id-set divergence only; respond-policy drift
> OQ-deferred to PR 7" and the misleading "is logged" clause dropped.
> No RFC §B edit was needed — the existing prose already speaks in
> terms of "divergent participant IDs", so the spec and code agree.
