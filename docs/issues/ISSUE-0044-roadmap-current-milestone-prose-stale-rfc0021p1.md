---
id: ISSUE-0044
summary: "ROADMAP.md 'Current milestone' prose still reads '1/3 PRs merged' for RFC 0021 P1; structured rows are correct"
status: resolved
severity: low
area: docs
created: 2026-05-06
closed: 2026-05-07
closed_pr: 267
refs:
  - ROADMAP.md
  - docs/rfcs/0021-pr-plan.md
  - docs/v0.3.0-plan.md
---

## Summary

[`ROADMAP.md:5`](../../ROADMAP.md#L5) "Current milestone" prose
still reads:

> *"… RFC 0021 P1 implementation in progress (1/3 PRs merged —
> PR 1 #256 Clock seam + temporal rendering; PR 2 next —
> now-anchor + recency rendering); …"*

After PRs #260 and #261 (both merged 2026-05-06), the truthful
state is "3/3 — RFC 0021 P1 complete". The structured rows at
[L55](../../ROADMAP.md#L55) and [L462](../../ROADMAP.md#L462)
were flipped to ⚠️ Partially Implemented (Phase 1) by PR #261;
the narrative paragraph was not.

## Context

Captured during the PR #261 deep review (Finding L3). The
"Last updated" line at [ROADMAP.md:3](../../ROADMAP.md#L3) was
set to 2026-05-06 in PR #258 but only mentions PR #256, not
#260 or #261.

The PR description for #261 did not list the Current-milestone
prose as in-scope, and `ROADMAP.md`'s narrative line is typically
refreshed in a separate cadence (it is a long, hand-curated
paragraph). The structured row at L55 + L462 is what
`make doc-status` reads; the narrative line is bookkeeping that
lags.

## Impact

- A reader who lands on the roadmap top-of-file gets a stale
  picture: they see "1/3 in progress" and look for PR 2/PR 3 work,
  unaware that the phase is complete.
- The "Last updated" line cites only PR #256, missing the two
  follow-ups that actually closed the phase.

## Proposed fix / investigation path

Two-line edit, ideally folded into the next ROADMAP-refresh pass
(or the v0.3.0 release-prep PR):

1. **L3** "Last updated" — append `, #260, #261` next to the
   existing `#256` reference for symmetry.
2. **L5** "Current milestone" — flip the RFC 0021 P1 fragment from
   *"in progress (1/3 PRs merged …)"* to something like
   *"Phase 1 complete (PRs #256, #260, #261); P2 — interaction
   tracker — next"*.

Defer to a refresh cadence rather than a one-off PR; the
structured rows are the source of truth and they are correct.

## Notes

> 2026-05-06 — captured during PR #261 deep review (Finding L3).
> Deliberately scoped out of PR #261 because the Current-milestone
> paragraph touches multiple RFCs and warrants a coordinated
> refresh, not a one-line edit.
