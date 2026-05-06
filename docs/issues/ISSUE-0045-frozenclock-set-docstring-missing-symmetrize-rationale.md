---
id: ISSUE-0045
summary: "FrozenClock.set docstring documents 'why unguarded' but does not explain why doc-only beats symmetrizing with advance"
status: open
severity: low
area: agents
created: 2026-05-06
refs:
  - agents/clock.py
  - docs/issues/ISSUE-0037-frozenclock-set-vs-advance-asymmetry.md
---

## Summary

The docstring added to
[`FrozenClock.set`](../../agents/clock.py#L100-L113) in PR #261
(closing ISSUE-0037) justifies *why* the method is unguarded —
re-anchoring across phases, DST resets — and tells callers to
construct a fresh `FrozenClock` if they want a guarded surface.
What it does not explain is *why this is preferable to* symmetrizing
with `advance` (i.e., rejecting `at < self._t`), which was
[ISSUE-0037's Option 2](ISSUE-0037-frozenclock-set-vs-advance-asymmetry.md).

## Context

Captured during the PR #261 deep review (Finding N1). A reader who
lands on the `FrozenClock.set` docstring without reading ISSUE-0037
sees the "why unguarded" rationale and accepts it, but may still
wonder "why not just make `set` reject backward jumps the way
`advance` does?" — the docstring closes one half of the loop but
not the other.

The asymmetry between `set` (unguarded) and `advance` (rejects
backward jumps) is a deliberate API choice, not an oversight. The
docstring should say so explicitly.

## Impact

- Pure documentation gap. No runtime effect.
- A future contributor who hits the asymmetry has to re-derive the
  rationale or open ISSUE-0037 to find it. Half a sentence in the
  docstring would close that loop forever.

## Proposed fix / investigation path

Append one sentence to the existing docstring along the lines of:

```
Why not symmetrize with advance() and reject at < self._t? Because
set() is the re-anchor primitive — Phase 2 changes, DST resets, and
test fixture rebuilds all need to move backward in time, and
forcing callers to construct a fresh FrozenClock for those cases
would be ergonomic noise without actually preventing the class of
bug advance() guards against (monotonic test sequences accidentally
rewinding mid-flow).
```

Truly a nit; the existing text is enough to land. Roll into the
next agents/clock.py touch (likely RFC 0021 P2's tracker work).

## Notes

> 2026-05-06 — captured during PR #261 deep review (Finding N1).
> Optional polish; not a blocker for any downstream work. Best
> done when `agents/clock.py` is next touched anyway, to avoid a
> docstring-only PR.
