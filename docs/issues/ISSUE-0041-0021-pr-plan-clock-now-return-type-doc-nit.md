---
id: ISSUE-0041
summary: "docs/rfcs/0021-pr-plan.md:57 says `Clock.now()` returns a `datetime`; RFC body and PR 1 impl return `float`"
status: resolved
severity: low
area: docs
created: 2026-05-06
closed: 2026-05-06
closed_pr: 261
refs:
  - docs/rfcs/0021-pr-plan.md
  - docs/rfcs/0021-persona-temporal-awareness.md
  - agents/clock.py
---

## Summary

[`docs/rfcs/0021-pr-plan.md:57`](../rfcs/0021-pr-plan.md#L57) reads:

> `Clock.now()` returns a `datetime` with timezone — no naive
> `datetime` in this RFC's surface.

This contradicts both the RFC body (§B specifies
`now() -> float`, epoch seconds UTC) and the PR 1 implementation
([`agents/clock.py`](../../agents/clock.py)), which returns
`float`. The PR plan blurb is wrong; the impl is right.

## Context

Captured during the PR #256 deep review (Doc nit, separate from
findings M1–M4). The line predates PR 1; it appears to reflect an
earlier draft of the Clock surface where `datetime` was the chosen
return type. By the time the RFC body settled on `float` (epoch
seconds UTC, with `now_iso() -> str` for the rendered form), the
PR-plan key-implementation-details bullet was not updated.

A reader who lands on the PR plan in isolation would build the
wrong mental model: they would expect `Clock.now()` to return a
`datetime` and `now_iso()` to be derived from it, when in fact the
relationship is the reverse.

## Impact

- Misleading for any future contributor who reads the PR plan
  without cross-referencing the RFC body.
- A PR 2 reviewer could read the line, observe that `WallClock.now()`
  returns `float`, and flag it as a regression — wasting cycles.

## Proposed fix / investigation path

Replace the line with one of:

1. **Match the RFC body**:
   > `Clock.now()` returns `float` epoch seconds (UTC). `now_iso()`
   > renders the timestamp in the configured persona timezone — no
   > naive `datetime` appears anywhere in this RFC's surface.

2. **Delete the line**: the surrounding bullets cover the relevant
   contract (no naive datetime; tz applied at render time), and the
   per-method types belong in the RFC body anyway.

Preferred: option 1, as a one-line edit during PR 3.

## Notes

> 2026-05-06 — captured during PR #256 review (Doc nit, called out
> separately from findings M1–M4 because it is a documentation fix
> rather than a code follow-up). Tracked for PR 3 specifically per
> the deep-review report.
