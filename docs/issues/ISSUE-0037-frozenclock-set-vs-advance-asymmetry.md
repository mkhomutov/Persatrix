---
id: ISSUE-0037
summary: "FrozenClock.advance rejects backward jumps but FrozenClock.set silently rewinds — document the contract or align"
status: open
severity: low
area: agents
created: 2026-05-06
refs:
  - agents/clock.py
  - docs/rfcs/0021-persona-temporal-awareness.md
  - docs/rfcs/0021-pr-plan.md
---

## Summary

[`FrozenClock.advance(-1)`](../../agents/clock.py#L91-L93) raises `ValueError`
with the explicit rationale that "a test cannot accidentally mask an
off-by-one in the rendering layer by reversing the clock"
([clock.py:74-77](../../agents/clock.py#L74-L77)). `FrozenClock.set(0)`
called from `at=1000.0` silently rewinds without rejection, which
defeats the same contract.

## Context

Captured during the PR #256 deep review (Finding M1). The asymmetry is
intentional in spirit — `set` is meant for explicit re-anchoring during
test setup phase 2, `advance` is for stepping through time mid-test —
but the two methods carry no docstring on `set` to make this clear,
and a careless `frozen.set(frozen.now() - 60)` mid-test silently breaks
the off-by-one guarantee the class advertises.

## Impact

- A test author who reaches for `set` mid-test (because `advance` won't
  let them rewind) circumvents the design intent of the seam without
  any signal that they have done so.
- The class docstring's "off-by-one cannot be masked" claim is
  weakened — strictly true for `advance`, false for `set`.

## Proposed fix / investigation path

Two options. Pick one for PR 3 review follow-ups:

1. **Doc-only**: Add a docstring to `FrozenClock.set` that explicitly
   says: "Use for explicit re-anchoring (e.g., resetting to a fresh
   epoch in a new test phase). Use `advance` for forward stepping
   within a single test phase." Cheap, preserves the legitimate
   re-anchoring use case.

2. **Tighten `set`**: Reject `at < self._t` with the same `ValueError`.
   Forces re-anchoring tests to construct a new `FrozenClock` instance,
   which is arguably the cleaner pattern anyway. More invasive but
   restores symmetry.

Option 1 is the lighter touch and matches the spirit of the existing
inline comments. Option 2 is a stronger guarantee but rewrites a few
existing test fixtures (none yet, since PR 1 is the introduction).

## Notes

> 2026-05-06 — captured during PR #256 review (Finding M1, marked
> "minor follow-up"). Not a merge blocker for #256; tracked for PR 3
> review follow-ups per the [PR plan's PR 3 scope](../rfcs/0021-pr-plan.md#pr-3-featurev030-rfc0021p1-close--review-follow-ups--rfc-phase-1-close).
