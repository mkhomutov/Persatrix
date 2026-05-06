---
id: ISSUE-0043
summary: "docs/v0.3.0-plan.md row 3 flipped to ✅ Merged but PR # and merge-date columns are still dashes"
status: open
severity: low
area: docs
created: 2026-05-06
refs:
  - docs/v0.3.0-plan.md
  - docs/rfcs/0021-pr-plan.md
---

## Summary

[`docs/v0.3.0-plan.md:70`](../v0.3.0-plan.md#L70) (RFC 0021 P1
implementation row) was flipped to `✅ Merged` in PR #261 but the
PR # and merge-date columns were left as `— | —`, breaking the
table convention for completed multi-PR rows.

## Context

Captured during the PR #261 deep review (Finding L2). The table's
instruction line at [L63](../v0.3.0-plan.md#L63) reads:

> *"Update this table as each step PR merges. Flip Status and add
> the GitHub PR number + merge date."*

Rows 0/1 follow that convention with `[#206]` / `2026-04-25`. Rows
2/4/5/6 use `— | —` because they are still 🔄 In progress
(multi-PR rolling). **Row 3 is the first multi-PR row to flip to
✅ Merged** — and it left the dashes in place rather than rolling
up the constituent PRs.

The convention isn't documented either way for multi-PR rows on
completion, but readers who land on this table get no lookup path
back to the implementing PRs.

## Impact

- A reader scanning the v0.3.0 plan for "what landed RFC 0021 P1?"
  has no clickable handle and must cross-walk through the RFC PR
  plan to reconstruct the answer.
- Sets a precedent that incomplete rollups are acceptable for
  multi-PR rows, which will compound as more multi-PR rows close
  out (RFC 0008 P3, RFC 0009 P2, RFC 0011 P3, etc.).

## Proposed fix / investigation path

Roll the three constituent PRs and the close date into row 3:

```
| 3 | 2 | RFC 0021 P1 implementation … | … | ✅ Merged | [#256](…), [#260](…), [#261](…) | 2026-05-06 |
```

While here, also document the multi-PR rollup convention in the
table preamble at [L63](../v0.3.0-plan.md#L63) so subsequent rows
follow the same pattern.

## Notes

> 2026-05-06 — captured during PR #261 deep review (Finding L2).
> Convenience polish, not a correctness bug — the per-PR detail is
> fully recoverable via the RFC 0021 PR plan. Worth fixing before
> the next multi-PR row closes (likely RFC 0008 P3) so the
> convention is set.
