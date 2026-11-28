---
id: ISSUE-0277
summary: "The weekly advisory audit reads the Python pins as Python 3.11 reads them. A pin that applies only from Python 3.12 is dropped without a word, and the agent image runs 3.12. So the versions that ship in the image and differ from CI's are the ones no audit reads. Today that is one package, numpy."
status: open
severity: low
area: ci
created: 2026-11-16
refs:
  - .github/workflows/scheduled-audit.yml
  - .github/python-constraints-providers.txt
  - Dockerfile.agent
  - scripts/checks/image_pins.py
  - docs/issues/ISSUE-0219-agent-image-is-not-what-ci-tests.md
  - docs/issues/ISSUE-0218-advisory-scanning-covers-rust-only.md
  - https://github.com/mkhomutov/Persatrix/pull/1137
---

# ISSUE-0277: The advisory audit does not read the pins of the image's Python

## Summary

The weekly audit (`.github/workflows/scheduled-audit.yml`) runs `pip-audit
--no-deps --disable-pip -r` on each constraints file, under Python 3.11.
pip-audit keeps a line whose marker holds on the Python it runs on and
drops the others. `.github/python-constraints-providers.txt` pins two
packages by Python version: `numpy` 2.4.6 below Python 3.12 and 2.5.4
from 3.12, `pandas` 2.3.3 below 3.14 and 3.1.1 from 3.14.

The agent image runs Python 3.12 and, since
[ISSUE-0219](ISSUE-0219-agent-image-is-not-what-ci-tests.md), installs
from that file. With the provider SDKs it holds `numpy` 2.5.4, which the
audit never reads.

## Context

The audit's own comment says so, and said that auditing the other
Pythons would follow the choice of the image's Python. That choice is
made: the image stays on 3.12, and CI tests 3.11 and 3.12.

`numpy` and `pandas` come with the watsonx SDK only, so an image built
without `AGENT_EXTRAS` holds neither.

## Impact

An advisory against `numpy` 2.5.4 that does not also cover 2.4.6 would
not fail the weekly audit, although the image with the provider SDKs
ships it. One package today; the set grows whenever a refresh resolves
another package differently by Python.

## Proposed fix / investigation path

Run the Python audit a second time on the image's Python: the same
command with `--python 3.12`, the minor read from `Dockerfile.agent` as
`tests/unit/python/test_python_312_job.py` reads it. The issue the audit
opens then names which Python a finding is on.

## Notes

> 2026-11-16 — left by the fix for
> [ISSUE-0219](ISSUE-0219-agent-image-is-not-what-ci-tests.md)
> ([#1137](https://github.com/mkhomutov/Persatrix/pull/1137)), which
> names it as what remains. Severity proposed from the tracker's
> precedent for the audit's gaps (low); not ruled by the maintainer.

> 2026-11-16 — the same gap in another check:
> [ISSUE-0283](ISSUE-0283-provider-sdks-are-run-on-python-311-only.md).
> The tests that run the provider SDKs run on Python 3.11 only, so the
> `numpy` pin this issue names is neither audited nor run on 3.12.

> 2026-11-28 — severity ruled `low`, as proposed: the maintainer's
> ruling at the
> [strategy review of 2026-11-28](../project-strategy-review-2026-11-28.md#3-the-maintainers-rulings),
> ratified by ruling (ab) of the
> [sequencing Amendment 2026-11-28](../v0.4.x-sequencing-2026-11-28.md#rulings).
