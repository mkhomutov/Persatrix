---
id: ISSUE-0038
summary: "test_clock.py and test_temporal_rendering.py use pytest.raises(Exception) for bad-tz cases — tighten to ZoneInfoNotFoundError"
status: resolved
severity: low
area: tests
created: 2026-05-06
closed: 2026-05-06
closed_pr: 261
refs:
  - tests/unit/python/test_clock.py
  - tests/unit/python/test_temporal_rendering.py
  - agents/clock.py
  - agents/temporal/rendering.py
---

## Summary

Two bad-timezone tests assert against the bare
[`Exception`](../../tests/unit/python/test_clock.py#L42)
[base class](../../tests/unit/python/test_temporal_rendering.py#L166)
rather than the specific exception type the production code raises
(`zoneinfo.ZoneInfoNotFoundError`, a subclass of `KeyError`).

## Context

Captured during the PR #256 deep review (Finding M2). Locations:

- [`tests/unit/python/test_clock.py:42`](../../tests/unit/python/test_clock.py#L42):
  `with pytest.raises(Exception): WallClock(tz="Not/A_Real_Zone")`
- [`tests/unit/python/test_temporal_rendering.py:166`](../../tests/unit/python/test_temporal_rendering.py#L166):
  `with pytest.raises(Exception): format_relative(NOW - 60, NOW, tz="Not/A_Real_Zone")`

The intent of both tests is "WallClock / format_relative reject an
unknown IANA zone". `pytest.raises(Exception)` succeeds if literally
any exception is raised — including unrelated bugs (an
`AttributeError` from a renamed attribute, an `ImportError` from a
moved module, a `TypeError` from a signature change).

## Impact

- A future refactor that breaks `WallClock.__init__` in an
  unrelated way (e.g., a typo in attribute access before the
  `ZoneInfo` call) would still pass these tests, masking the
  regression.
- The "tz validation happens at construction" contract the PR
  claims is not actually pinned — only "something raises".

## Proposed fix / investigation path

Replace both call sites with the specific exception type:

```python
import zoneinfo

# test_clock.py:42
with pytest.raises(zoneinfo.ZoneInfoNotFoundError):
    WallClock(tz="Not/A_Real_Zone")

# test_temporal_rendering.py:166
with pytest.raises(zoneinfo.ZoneInfoNotFoundError):
    format_relative(NOW - 60, NOW, tz="Not/A_Real_Zone")
```

Optional defensive add: a `match=` regex pinning the zone name in
the error message would also catch a future "wrong zone reported in
error" regression. Probably overkill for this surface.

## Notes

> 2026-05-06 — captured during PR #256 review (Finding M2, marked
> "minor follow-up"). Not a merge blocker for #256; tracked for PR 3
> review follow-ups.
