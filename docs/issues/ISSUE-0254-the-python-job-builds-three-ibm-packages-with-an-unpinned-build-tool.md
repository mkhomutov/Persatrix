---
id: ISSUE-0254
summary: "The required Python job now builds three packages from source: `ibm-cos-sdk`, `ibm-cos-sdk-core` and `ibm-cos-sdk-s3transfer`, which the watsonx SDK needs and which publish no wheels. pip builds each with the newest setuptools, which CI's pins do not reach. A setuptools release that breaks their build fails the job on every open PR at once."
status: open
severity: low
area: ci
created: 2026-11-14
refs:
  - https://github.com/mkhomutov/Persatrix/pull/1118
  - Makefile
  - .github/workflows/ci.yml
  - .github/python-constraints-providers.txt
  - docs/issues/ISSUE-0171-build-dependencies-float-past-the-ci-pins.md
  - docs/issues/ISSUE-0220-gemini-and-watsonx-sdks-sit-outside-every-gate.md
---

# ISSUE-0254: The Python job builds three IBM packages with an unpinned build tool

## Summary

The fix for
[ISSUE-0220](ISSUE-0220-gemini-and-watsonx-sdks-sit-outside-every-gate.md)
installs the optional provider SDKs in CI's required Python job
(`make build-agents-providers`). The watsonx SDK needs three packages that
are published as source only: `ibm-cos-sdk`, `ibm-cos-sdk-core` and
`ibm-cos-sdk-s3transfer`, each 2.14.3 at the pins of 2026-11-14. pip
builds them on the runner, in a separate, temporary environment, and puts
the newest setuptools there. The pins CI installs from do not reach that
environment.

This is the gap of
[ISSUE-0171](ISSUE-0171-build-dependencies-float-past-the-ci-pins.md),
which records it for the agents' own package. It now also covers three
third-party packages whose build files the project does not control.

## Context

Found in the review of #1118, and checked again there.

- The CI log of the Python job shows the three downloaded as `.tar.gz`
  and "Building wheel for …" for each. The job ran pip 26.3.
- The three declare no build tool, so pip uses its fallback,
  `setuptools>=40.8.0`, at the newest release.
- The constraints file does pin setuptools (84.2.1), but for the install
  itself, not for a build.

The review proposed exporting the constraints file as `PIP_CONSTRAINT`
for the provider install. That does not work on CI's pip, as ISSUE-0171
already says. Measured on pip 26.3, building `ibm-cos-sdk-core` 2.14.3
with a file that pins `setuptools==80.0.0`:

| How the pin was given | setuptools in the build |
|---|---|
| not given | 84.2.1 (the newest) |
| `PIP_CONSTRAINT` | 84.2.1 |
| `--build-constraint` | 80.0.0 |

## Impact

Proposed low, as ISSUE-0171: low likelihood, wide effect. A setuptools
release that breaks one of the three builds fails
`make build-agents-providers`, and with it the required Python job on
every open PR, until someone acts. The weekly refresh would not warn of
it: the build environment floats in every run, so the refresh and CI fail
at the same moment. The severity is proposed here, not ruled.

## Proposed fix / investigation path

1. Pass the constraints file as a build constraint in
   `make build-agents-providers`:
   `--build-constraint $(PYTHON_CONSTRAINTS_PROVIDERS)`. It is the fix
   ISSUE-0171 proposes for `make build-agents`, with the same cost: pip
   refuses the option when build isolation is off. Doing both in one
   change is the natural shape.
2. Or move the four provider steps to a job of their own. A broken build
   would then fail that job and not the Python checks beside it. Whether
   the new job is required is a repository setting, so the choice is the
   maintainer's.

## Notes

> 2026-11-14 — filed from the review of #1118.
