---
id: ISSUE-0171
summary: "CI's Python pins (#1003) do not reach the environment pip builds the editable agents package in, so setuptools there is always the newest release inside `>=75.0`. pip 26.2 and later ignore `-c` and `PIP_CONSTRAINT` in that isolated build on purpose; only `--build-constraint` (pip 25.3 or later) reaches it. A setuptools release that breaks this package's editable build would fail `make build-agents` in every CI job at once, the way OpenTelemetry 1.45.0 failed every PR's type check until #1002, but at install time."
status: open
severity: low
area: ci
created: 2026-09-26
refs:
  - https://github.com/mkhomutov/Persatrix/pull/1003
  - https://github.com/mkhomutov/Persatrix/pull/1002
  - Makefile
  - agents/pyproject.toml
  - Dockerfile.agent
---

# ISSUE-0171: The build dependencies of the agents package float past CI's pins

## Summary

[#1003](https://github.com/mkhomutov/Persatrix/pull/1003) makes every CI job
install the agents' Python dependencies at committed versions: `make
build-agents` passes `.github/python-constraints.txt` to pip with `-c`. Those
pins do not reach one step of that install. To install the agents package
itself, pip first builds it in a separate, temporary environment, and there it
installs whatever `[build-system].requires` in `agents/pyproject.toml` asks
for (`setuptools>=75.0`) at the newest release. So a new setuptools release
still reaches CI the minute it is published.

## Context

Found as F-7 in the review of #1003. The review suggested exporting the
constraints file as `PIP_CONSTRAINT`, which older pip passed on to the build
environment. That no longer works in CI, which takes the newest pip (`python
-m pip install --upgrade pip`). From pip 26.2 on, pip tells the build
environment to ignore every regular constraint, whether it came from `-c`,
`PIP_CONSTRAINT` or a config file. Before 26.2, `PIP_CONSTRAINT` still reaches
it (from 25.3 with a deprecation warning). From 26.2 on, the only thing that
reaches it is the `--build-constraint` option, which pip added
in 25.3 (checked: `pip install --help` lists it in 25.3 and not in 25.2).

The constraints file already pins setuptools (84.0.0 when #1003 was opened),
but only because `grpcio-tools` depends on it at run time. Nothing guarantees
that line stays: if `grpcio-tools` drops the dependency, the pin disappears
from the file without anyone deciding it should.

## Impact

Low likelihood, wide effect. A setuptools release that breaks this package's
editable build fails the install step of every CI job that runs `make
build-agents`, on every open PR, until someone caps the range. That is how
OpenTelemetry 1.45.0 failed every PR's type check until #1002, but here at
install time. This package is more exposed than most:
`[tool.setuptools.package-dir]` maps `agents/` onto the `persatrix_agents`
import path, and its package list is written out by hand.

The weekly refresh from #1003 would not warn of it. It tries the newest
releases of the pinned packages, but the build environment floats in every
run anyway, so a breaking setuptools release fails the refresh and CI at the
same moment.

The agent image has the same gap, and a wider one. `Dockerfile.agent` installs
the package with `pip install ./agents` and no constraints at all, so a
breaking setuptools release also breaks `make docker-up`. CI does not build
the image, so nothing would warn of it. #1003 lists the image under "Not in
this PR"; no other issue tracks it, and the steps below do not reach it.

## Suggested fix

1. In `make build-agents`, also pass the constraints file as a build
   constraint: `--build-constraint ../$(PYTHON_CONSTRAINTS)`. pip refuses that
   option when build isolation is off (`--no-build-isolation`, or the same
   setting in the environment or a config file), so anyone who builds without
   isolation would see `make build-agents` fail where it works today.
2. Add a unit test that every package in `[build-system].requires` has a pin
   in the constraints file, and that the pin meets the range there
   (`setuptools>=75.0`), so the setuptools pin no longer depends on
   `grpcio-tools`. If the test fails one day, the file needs the build
   requirements as a second input to `uv pip compile`.
3. Decide what happens with an older pip. `--build-constraint` is an unknown
   option before 25.3, so `make build-agents` would fail outright there; the
   shared `.venv` had pip 25.0.1 when this was filed. Either upgrade pip
   first in the target; or check the version and say what to run; or pass
   `--build-constraint` on pip 25.3 or later and export `PIP_CONSTRAINT` below
   that, since older pip still passes it to the build. Pinning pip
   itself would also close the other gap #1003 lists: the `--upgrade pip` step
   takes the newest pip.
