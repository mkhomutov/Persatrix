---
id: ISSUE-0042
summary: "test_empty_string_tz_falls_back_to_default asserts on UTC offset suffix only — does not pin DEFAULT_TIMEZONE identity"
status: resolved
severity: low
area: tests
created: 2026-05-06
closed: 2026-05-07
closed_pr: 267
refs:
  - tests/unit/python/test_clock.py
  - agents/clock.py
  - docs/issues/ISSUE-0039-wallclock-empty-tz-silent-utc-fallback.md
  - docs/rfcs/0021-pr-plan.md
---

## Summary

[`test_empty_string_tz_falls_back_to_default`](../../tests/unit/python/test_clock.py#L57-L66)
(added in PR #261 to close ISSUE-0039) does not actually pin "fell
back to *DEFAULT_TIMEZONE* specifically" — only "fell back to a
UTC-equivalent offset". The test would still pass under several
realistic regressions the issue claimed it would catch.

## Context

Captured during the PR #261 deep review (Finding L1). The current
test body is:

```python
def test_empty_string_tz_falls_back_to_default(self) -> None:
    clock = WallClock(tz="")
    assert clock.now_iso().endswith("+00:00")
    assert str(zoneinfo.ZoneInfo(DEFAULT_TIMEZONE)) == DEFAULT_TIMEZONE
```

Two weaknesses:

1. **Line 65** (`endswith("+00:00")`) is the only behavioral
   assertion — and it is already covered byte-for-byte by
   `test_now_iso_default_zone_is_utc` at L33-L40. Any zone aliased
   to UTC offset (`Etc/UTC`, `Etc/GMT`, …) would also pass, so the
   drift this test claims to catch (PR 2's schema layer flipping
   `""` to `Europe/Berlin` instead of `UTC`, say) would still
   satisfy the assertion in winter.
2. **Line 66** (`assert str(zoneinfo.ZoneInfo(DEFAULT_TIMEZONE)) == DEFAULT_TIMEZONE`)
   is a stdlib roundtrip check on the constant itself — it has no
   relationship to `WallClock`. If `WallClock` were rewritten
   tomorrow to ignore `tz` entirely, line 66 would still pass.

The original [issue text](ISSUE-0039-wallclock-empty-tz-silent-utc-fallback.md#L57-L60)
proposed `assert clock.tz == DEFAULT_TIMEZONE`, which requires
exposing `_tz` (or a `tz` property), which the impl currently does
not do.

## Impact

- The regression test does not actually pin the contract it claims
  to pin. ISSUE-0039 is "closed" in the index but the underlying
  drift it was meant to catch (PR 2's schema layer changing the
  empty-string fallback behavior) would not surface in CI.
- A future reader who lands on the test sees an assertion that
  *looks* meaningful but is a no-op pin, which erodes trust in the
  surrounding test file.

## Proposed fix / investigation path

Pick one of the two non-invasive options:

1. **Compare against a known-good `WallClock()` instance** — same
   epoch ⇒ same render iff same tz:

   ```python
   default_clock = WallClock()
   empty_clock = WallClock(tz="")
   assert default_clock.now_iso()[:19] == empty_clock.now_iso()[:19]
   ```

   Imperfect on a clock-tick boundary; consider a small retry or
   slice off the seconds.

2. **Use `FrozenClock` for a deterministic comparison** (cleanest):

   ```python
   fc_default = FrozenClock(at=1_714_055_520.0)
   fc_empty = FrozenClock(at=1_714_055_520.0, tz="")
   assert fc_default.now_iso() == fc_empty.now_iso()
   ```

3. **Or expose `tz` as a property** on `WallClock` and assert
   `clock.tz == DEFAULT_TIMEZONE` directly — invasive but matches
   the original ISSUE-0039 proposal exactly.

Either option closes the gap; (2) is preferred for determinism.

## Notes

> 2026-05-06 — captured during PR #261 deep review (Finding L1).
> Not a merge blocker — the test does fail loudly if the fallback
> breaks in the obvious way (e.g., `WallClock(tz="")` raising), but
> it is weaker than ISSUE-0039 intended. Best closed in a one-line
> follow-up before PR 2 schema work lands, so the regression test
> earns its keep.
