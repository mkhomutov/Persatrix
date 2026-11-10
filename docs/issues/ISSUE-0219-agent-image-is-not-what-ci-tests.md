---
id: ISSUE-0219
summary: "No CI job builds the product images. The agent image runs Python 3.12 and installs the newest allowed version of every dependency, while every CI job tests Python 3.11 at pinned versions. One unit test already fails on 3.12, and nothing would show a product fault of the same kind before a release."
status: open
severity: medium
area: ci
created: 2026-11-10
refs:
  - Dockerfile.agent
  - Dockerfile.orchestrator
  - .github/workflows/ci.yml
  - .github/python-constraints.txt
  - tests/unit/python/test_rfc0029_callsite_refactor.py
  - docs/issues/ISSUE-0171-build-dependencies-float-past-the-ci-pins.md
  - docs/issues/ISSUE-0178-runner-rule-checked-only-for-go.md
  - docs/issues/ISSUE-0210-agent-package-runs-under-two-import-roots.md
---

# ISSUE-0219: The agent image is not what CI tests, and no CI job builds it

## Summary

What CI proves and what ships differ in three ways:

| | CI | Agent image |
|---|---|---|
| Python | 3.11 in every job | `python:3.12-slim` |
| Dependency versions | pinned by `.github/python-constraints.txt` | `pip install ./agents`, no constraints, newest release in each range |
| Built by CI | — | never; the only Docker step is the `.dockerignore` check |

## Context

- On Python 3.12 the unit test
  `test_personal_tier_latency_harness_runs` fails with an
  `AttributeError` inside `dataclasses`. The cause is in the test: it
  loads a module without registering it in `sys.modules`. The point is
  that the suite is not green on the interpreter that ships, and nobody
  is told.
- The same gap hides
  [ISSUE-0210](ISSUE-0210-agent-package-runs-under-two-import-roots.md),
  which only the installed package shows.
- [ISSUE-0171](ISSUE-0171-build-dependencies-float-past-the-ci-pins.md)
  covers build dependencies that float past the pins, and notes the
  unpinned image install in passing.
  [ISSUE-0178](ISSUE-0178-runner-rule-checked-only-for-go.md) covers the
  tests that need a running orchestrator and run in no gate.

## Impact

A dependency release, or a Python 3.12 behaviour, can break the shipped
agent while every check on `main` is green. It would be found by whoever
next runs a demo.

## Proposed fix / investigation path

- One CI job that runs `docker compose build` and the offline demo's
  smoke test.
- Pass the constraints file to the image's `pip install`.
- Use one Python version for CI and the image, or test both.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; the 3.12 failure was reproduced on Python 3.12.13, the rest
> confirmed by reading. No image was built.
