---
id: ISSUE-0278
summary: "The release sweep's offline smoke gate runs `make demo-autonomous`, whose last step only prints a warning when the roundtable does not convene, and which never looks at whether any agent posted. The gate therefore passes on a stack that started and did nothing."
status: open
severity: low
area: release
created: 2026-11-16
refs:
  - scripts/release/sweep.py
  - Makefile
  - scripts/checks/offline_smoke.py
  - docs/methodology/release-cycle.md
  - docs/issues/ISSUE-0219-agent-image-is-not-what-ci-tests.md
---

# ISSUE-0278: The release sweep's offline smoke passes when nothing convenes

## Summary

`scripts/release/sweep.py` has an opt-in gate, "offline smoke", that runs
`make demo-autonomous`. The release cycle asks for it once, in the tag
pull request. The target fails when the stack does not become healthy
and when the roundtable cannot be armed. Its last step, the convening,
tries twenty times and then prints "Auto-convene did not land yet" and
ends with status 0. When the convening does land, nothing checks that an
agent then posted.

## Context

The target is written as a demo: it leaves a person with a running stack
and a hint. As a gate it answers "did the stack come up", and the
release cycle reads it as "the whole stack round-trips".

The fix for
[ISSUE-0219](ISSUE-0219-agent-image-is-not-what-ci-tests.md) added
`make smoke-offline`: the same target followed by
`scripts/checks/offline_smoke.py`, which fails unless at least two
registered agents have posted three messages in the roundtable. The
weekly image run uses it. The release sweep was left as it was.

## Impact

A release whose agents start and cannot complete a turn on the mock
provider would pass the sweep's smoke. The weekly run on `main` would
catch it, within a week and outside the release pull request.

## Proposed fix / investigation path

Point the gate at `make smoke-offline` in `scripts/release/sweep.py`,
and the release cycle's sentence and the testing strategy's row with it.

## Notes

> 2026-11-16 — found while giving the offline demo a verdict for
> [ISSUE-0219](ISSUE-0219-agent-image-is-not-what-ci-tests.md). Left as
> it is there: the release tooling is the release process's to change.
> Severity proposed (low); not ruled by the maintainer.
