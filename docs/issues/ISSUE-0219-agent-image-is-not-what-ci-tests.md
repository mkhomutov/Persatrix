---
id: ISSUE-0219
summary: "No CI job builds the product images. The agent image runs Python 3.12 and installs the newest allowed version of every dependency, while every CI job tests Python 3.11 at pinned versions. Nothing shows a fault that only the image has before merge; the first step that builds the images is an opt-in smoke run in the release PR."
status: resolved
severity: medium
area: ci
created: 2026-11-10
closed: 2026-11-16
refs:
  - Dockerfile.agent
  - Dockerfile.orchestrator
  - .github/workflows/ci.yml
  - .github/workflows/scheduled-image-smoke.yml
  - .github/python-constraints.txt
  - .github/python-constraints-providers.txt
  - scripts/checks/image_pins.py
  - scripts/checks/offline_smoke.py
  - docs/methodology/branch-protection.json
  - docs/issues/ISSUE-0277-advisory-audit-does-not-read-the-pins-of-the-images-python.md
  - docs/issues/ISSUE-0278-release-sweeps-offline-smoke-passes-when-nothing-convenes.md
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

> 2026-11-11 — the fix for
> [ISSUE-0210](ISSUE-0210-agent-package-runs-under-two-import-roots.md)
> ([#1098](https://github.com/mkhomutov/Persatrix/pull/1098)) adds a step
> to the Python job that starts the installed package from an empty
> folder, so that fault no longer needs the image to show. The job's
> install is an editable one. The image itself is still built and
> started by no CI job, which is what this issue is about.

> 2026-11-14 — a constraints file for the image install now exists. The
> fix for
> [ISSUE-0220](ISSUE-0220-gemini-and-watsonx-sdks-sit-outside-every-gate.md)
> adds `.github/python-constraints-providers.txt`: the pins of
> `.github/python-constraints.txt` plus the optional provider SDKs
> (`google-genai`, `ibm-watsonx-ai`) and what only they need. It is
> resolved for Python 3.11 and later on every platform, so the image's
> Python 3.12 can install from it, and since it holds every pin of the
> first file one `-c` serves an image built with or without
> `AGENT_EXTRAS`. Nothing here was changed to use it: `Dockerfile.agent`
> still installs the newest release in each range, and the demo images
> that set `AGENT_EXTRAS` still take the SDKs unpinned. That half of
> ISSUE-0220 is left with this issue.
>
> Two things to know when the image does install from it. The file pins
> two packages differently by Python version: `numpy` 2.4.6 below Python
> 3.12 and 2.5.4 from 3.12, `pandas` 2.3.3 below 3.14 and 3.1.1 from
> 3.14. CI's Python 3.11 installs the first of each and the image's 3.12
> would install the second `numpy`, so the two still differ by that one
> package. And an advisory audit run on CI's Python 3.11 reads the file
> as that Python sees it, so it would not see the 3.12 pins: they stay
> unaudited until an audit also runs on the image's Python
> ([ISSUE-0218](ISSUE-0218-advisory-scanning-covers-rust-only.md) tracks
> the weekly audit).

> 2026-11-16 — resolved, on the maintainer's three choices.
>
> **A weekly run on `main`, plus a manual trigger.**
> `.github/workflows/scheduled-image-smoke.yml` builds the product images
> every Monday and runs two checks, each a `make` target: `make
> image-pins-check` and `make smoke-offline`. A failure opens or updates
> one GitHub issue that names the check and its command. No job on a pull
> request builds an image, as before. `make smoke-offline` is the offline
> demo with a verdict: `make demo-autonomous` only warns when the
> convening does not land, so the new target fails unless at least two of
> the roundtable's agents have posted three messages.
>
> **The image's install is pinned.** `Dockerfile.agent` passes
> `.github/python-constraints-providers.txt` to its `pip install`, with
> and without `AGENT_EXTRAS`. Built from `main` on this day, the image
> held 22 of 58 packages at another version than the pins (23 of 80 with
> both provider SDKs): `openai` 3.51.0 for the pinned 3.37.0, `grpcio`
> 1.85.1 for 1.84.1, the OpenTelemetry packages a patch release on. Built
> from the changed file, all 58, and all 80, are at their pin. The
> comparison runs inside the image, on its own Python, because the file
> pins `numpy` differently from Python 3.12.
>
> **Both Python versions are tested in CI.** A new job, `Python 3.12
> (unit tests)`, runs the unit suite on the image's Python, installed
> from the same pins. Every other job stays on 3.11, the oldest Python
> the package supports. Run here on both from the pins, on macOS: 8,912
> passed and 19 skipped on each, with the same outcome for every one of
> the 8,931 tests. The job is not required: which checks `main` requires
> is the owner's setting, and `docs/methodology/branch-protection.json`
> now names the job under `not_required`, so that a job outside the
> required set is a recorded choice. The image stays on
> `python:3.12-slim`; a move to another minor is made by hand, with the
> job.
>
> What remains after this, none of it this issue's:
>
> - The weekly advisory audit reads the pins as Python 3.11 reads them,
>   so the pins that apply only on the image's 3.12 are not audited
>   ([ISSUE-0277](ISSUE-0277-advisory-audit-does-not-read-the-pins-of-the-images-python.md)).
>   Today that is one package, `numpy` 2.5.4.
> - The tools that build a package during the install are still not
>   pinned, in the image as in CI
>   ([ISSUE-0171](ISSUE-0171-build-dependencies-float-past-the-ci-pins.md)).
> - Pinning the image holds it at the pinned `opentelemetry-api` 1.45.1,
>   the release the weekly dependency audit reports; an unpinned build
>   had moved to 1.45.2. The image follows when that pin moves.
> - The release sweep's own "offline smoke" gate still runs `make
>   demo-autonomous`, without the verdict
>   ([ISSUE-0278](ISSUE-0278-release-sweeps-offline-smoke-passes-when-nothing-convenes.md)).
> - The weekly workflow has not run: a scheduled workflow runs only from
>   `main`. Its two targets were run by hand with Docker, and tests hold
>   its steps and the text of the issue it opens. Its first run needs a
>   manual trigger after the merge.
