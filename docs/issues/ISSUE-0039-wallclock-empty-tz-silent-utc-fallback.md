---
id: ISSUE-0039
summary: "WallClock(tz=\"\") silently falls back to UTC via `tz or DEFAULT_TIMEZONE`; behavior unpinned by tests"
status: resolved
severity: low
area: agents
created: 2026-05-06
closed: 2026-05-06
closed_pr: 261
refs:
  - agents/clock.py
  - tests/unit/python/test_clock.py
  - docs/rfcs/0021-persona-temporal-awareness.md
  - docs/rfcs/0021-pr-plan.md
---

## Summary

[`WallClock.__init__`](../../agents/clock.py#L64) uses
`ZoneInfo(tz or DEFAULT_TIMEZONE)`. An empty string `""` is falsy in
Python, so `WallClock(tz="")` silently coerces to UTC instead of
raising `ZoneInfoNotFoundError` (which is what `WallClock(tz=" ")` —
a space — does). This implicit contract is reasonable for hand-typed
operator config (an empty `tz: ""` line is plausibly intent for "no
override") but is not documented or pinned by tests.

## Context

Captured during the PR #256 deep review (Finding M3). RFC 0021 PR 2
adds a `persona.timezone` schema field with a default of `UTC`
([0021-pr-plan.md:91](../rfcs/0021-pr-plan.md#L91)). Once that schema
exists, the schema layer will normalize the tz value before it
reaches `WallClock`, and the empty-string surface drift here becomes
a non-issue. **Until then**, the current behavior is load-bearing for
any caller that passes through an unvalidated config string.

## Impact

- Operator confusion if `WallClock(tz="")` is debugged: the value
  goes in empty, the clock reports UTC times, and there's no error
  message explaining why.
- If PR 2's schema validation lands and disallows empty strings, a
  caller relying on the current `""` → UTC fallback would start
  raising. This could surface as a deployment regression on a
  previously-tolerated config value.

## Proposed fix / investigation path

One-line test pinning the current behavior, so PR 2's schema work
either preserves the contract intentionally or surfaces the drift
loudly:

```python
def test_wallclock_empty_string_tz_falls_back_to_utc() -> None:
    # Empty string is falsy → DEFAULT_TIMEZONE.
    # See ISSUE-0039 — pinned so PR 2's persona.timezone schema
    # work surfaces any inconsistency.
    clock = WallClock(tz="")
    assert clock.tz == DEFAULT_TIMEZONE
```

Optional: tighten `WallClock.__init__` to `ZoneInfo(tz)` for any
non-`None` `tz`, so empty-string is rejected at construction.
**Not recommended without coordination with PR 2's schema layer** —
two layers rejecting the same thing is fine, but flipping behavior
on a v0.3.0 boundary should be deliberate.

## Notes

> 2026-05-06 — captured during PR #256 review (Finding M3, marked
> "minor follow-up"). Not a merge blocker for #256; tracked for PR 3
> review follow-ups.
