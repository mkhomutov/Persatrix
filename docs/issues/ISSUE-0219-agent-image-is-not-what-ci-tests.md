---
id: ISSUE-0219
summary: "No CI job builds the product images. The agent image runs Python 3.12 and installs the newest allowed version of every dependency, while every CI job tests Python 3.11 at pinned versions. Nothing shows a fault that only the image has before merge; the first step that builds the images is an opt-in smoke run in the release PR."
status: open
severity: medium
area: ci
created: 2026-11-10
refs:
  - Dockerfile.agent
  - Dockerfile.orchestrator
  - .github/workflows/ci.yml
  - .github/python-constraints.txt
  - scripts/release/sweep.py
  - Makefile
  - docs/methodology/release-cycle.md
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

- Leaving image builds out of CI is deliberate: the header of
  `.github/workflows/ci.yml` says full product-image builds are excluded.
- The first step that builds and starts the images is the "offline
  smoke" gate of `make release-sweep`, which runs `make demo-autonomous`.
  It is opt-in, and the release cycle asks for it once, in the tag PR.
  So a fault that only the image has can sit on `main` for a whole
  release.
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
agent while every check on `main` is green. It would be found at the
release PR's smoke run, if that is run, or by whoever next runs a demo.

## Proposed fix / investigation path

- One CI job that runs `docker compose build` and the offline demo's
  smoke test.
- Pass the constraints file to the image's `pip install`.
- Use one Python version for CI and the image, or test both.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading. No image was built. A first draft
> cited a unit test as failing on Python 3.12. Review showed that the
> failure depends on the order the test files run in, not on the
> interpreter;
> [ISSUE-0233](ISSUE-0233-latency-harness-test-fails-when-its-file-runs-alone.md)
> records it.
