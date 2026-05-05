---
id: ISSUE-0024
summary: "tests/unit/python/ full-suite run hangs indefinitely; per-file runs pass"
status: open
severity: medium
area: tests
created: 2026-05-05
refs:
  - tests/unit/python/
  - docs/rfcs/0011-pr-plan.md
---

## Summary

Running the full Python unit suite via
`python -m pytest tests/unit/python/ -x --tb=short -q` hangs without
producing output (had to be terminated manually after >60 s with no
progress). Running the same files in isolation
(`tests/unit/python/test_http_channel_publisher.py` and
`tests/unit/python/test_channel_publish_rest.py`) completes in under
1 s with all tests passing.

## Context

Observed during PR #250 review-fix verification on
`feature/v030-rfc0011-grpc-dispatcher`. The narrow per-file invocation
finished cleanly:

```
tests\unit\python\test_http_channel_publisher.py .........  [ 60%]
tests\unit\python\test_channel_publish_rest.py ......       [100%]
============================= 15 passed in 0.82s ==============================
```

The full-suite invocation produced zero output and exited with code 1
only after manual termination — pytest's collection phase or one of the
other test modules appears to block indefinitely (no `pytest-timeout`
default is set on suite-wide runs, even though the dependency is
installed).

Likely suspects to investigate (not yet bisected):

- A test that opens a real network/loopback socket without a hard
  timeout and waits on a peer that is never connected.
- An async fixture with a missing `await runner.cleanup()` in an
  exception path.
- A `pytest-asyncio` event-loop scope mismatch with one of the newer
  loopback-server fixtures (`captured_server` in
  `test_http_channel_publisher.py` is a candidate, though it does not
  hang in isolation).

## Impact

- CI / local pre-commit cannot reliably run the full Python unit suite
  in a single invocation; reviewers fall back to per-file runs and lose
  the cross-module collection signal.
- TDD loop is degraded — a regression in one module is not caught when
  contributors only re-run the file they touched.

## Proposed fix / investigation path

1. Re-run with `-p no:cacheprovider --timeout=10 --timeout-method=thread`
   (pytest-timeout is in `pyproject.toml` already) to force the hang to
   surface as a per-test failure with traceback.
2. Bisect with `--collect-only` first to confirm whether collection or
   execution is the blocker.
3. If a specific async fixture is implicated, audit
   `tests/unit/python/conftest.py` for event-loop scope settings
   (`asyncio_default_fixture_loop_scope`).

## Notes

> 2026-05-05 — captured during PR #250 review-fix verification. Do not
> block PR #250 on this; the timeout / unify-constant fixes themselves
> were verified per-file.
