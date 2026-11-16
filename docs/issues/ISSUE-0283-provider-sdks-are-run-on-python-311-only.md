---
id: ISSUE-0283
summary: "The agent image runs Python 3.12, and an image built with the Gemini or watsonx SDK runs those SDKs there. No check runs them on 3.12: the 17 tests that need a real SDK run on Python 3.11 only, the 3.12 job installs no SDK and skips them, the weekly smoke starts the image without SDKs, and the weekly refresh of the pins tries new releases on 3.11."
status: open
severity: low
area: ci
created: 2026-11-16
refs:
  - .github/workflows/ci.yml
  - .github/workflows/python-constraints-refresh.yml
  - .github/workflows/scheduled-image-smoke.yml
  - .github/python-constraints-providers.txt
  - Makefile
  - Dockerfile.agent
  - docs/issues/ISSUE-0219-agent-image-is-not-what-ci-tests.md
  - docs/issues/ISSUE-0220-gemini-and-watsonx-sdks-sit-outside-every-gate.md
  - docs/issues/ISSUE-0277-advisory-audit-does-not-read-the-pins-of-the-images-python.md
  - https://github.com/mkhomutov/Persatrix/pull/1137
---

# ISSUE-0283: The provider SDKs are run on Python 3.11 only

## Summary

Since the fix for
[ISSUE-0219](ISSUE-0219-agent-image-is-not-what-ci-tests.md) the unit
suite runs on the Python the agent image runs, 3.12, from the pins the
image installs. That job installs the plain pins. The two optional
provider SDKs, Gemini and watsonx, are a second set of pins, and nothing
runs them on 3.12.

## Context

Where the SDKs are exercised, read in the workflows and the Makefile:

| Check | Python | The SDKs |
|---|---|---|
| The Python job's last four steps (`make build-agents-providers`, `lint-providers`, `test-providers`, `check-licenses-providers`) | 3.11 | installed, type-checked, and run: 17 tests against the real SDKs with the network closed |
| The `Python 3.12 (unit tests)` job | 3.12 | not installed; the 17 tests are skipped |
| `make image-pins-check`, weekly | 3.12, in the image | installed in the second image it builds, and each package compared with its pin. Nothing is imported or called |
| `make smoke-offline`, weekly | 3.12, in the image | not installed: the offline stack builds the image without `AGENT_EXTRAS` |
| The weekly refresh of the pins | 3.11 | new releases are installed and run on 3.11 before the pins move |

So three things reach an image built with `AGENT_EXTRAS` that no check
has run on its Python: the Gemini adapter, the watsonx adapter, and the
pins that apply only from 3.12. Today that is one pin, `numpy` 2.5.4,
which comes in with the provider SDKs, through `pandas`; on 3.11 the pin
is 2.4.6.

The review of pull request 1137 ran the 17 tests on a Python 3.12
environment built from the pins and reports that all pass. That is one
run on one day, not a check.

## Impact

A release of an SDK, or of something it needs, that breaks on 3.12 and
not on 3.11 would pass every check, move the pins through the weekly
refresh, and reach the next image built with that SDK. The first sign
would be an agent that fails when it calls Gemini or watsonx. The demo
stacks for those two providers build such images.

## Proposed fix / investigation path

Two more steps in the `Python 3.12 (unit tests)` job would close it:
`make build-agents-providers`, then `make test-providers`. That widens a
job the maintainer chose as "the unit suite on the image's Python", so
it is a choice and not a correction. The job is not required, and the
refresh would still try new pins on 3.11 first; running the refresh's
provider steps on 3.12 as well is the other half.

[ISSUE-0277](ISSUE-0277-advisory-audit-does-not-read-the-pins-of-the-images-python.md)
is the same gap in the advisory audit: it reads the pins as 3.11 reads
them.

## Notes

> 2026-11-16 — found by the review of pull request 1137 and filed by the
> maintainer's decision after it: the 3.12 job stays as chosen, and this
> is recorded. Severity proposed from the tracker's precedent
> ([ISSUE-0277](ISSUE-0277-advisory-audit-does-not-read-the-pins-of-the-images-python.md),
> low: a check that reads Python 3.11 where the image runs 3.12); not
> ruled by the maintainer.
