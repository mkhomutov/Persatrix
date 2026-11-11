---
id: ISSUE-0228
summary: "CONTRIBUTING's Quick Start begins with `make build-agents`, which runs a bare `pip3 install` and never mentions a virtual environment. Passing `PYTHON=.venv/bin/python` does not help, because the recipe installs through the separate `PIP` variable."
status: open
severity: low
area: docs
created: 2026-11-10
refs:
  - CONTRIBUTING.md
  - Makefile
  - CLAUDE.md
  - docs/methodology/automation-catalogue.md
---

# ISSUE-0228: The contributor Quick Start installs into the system Python

## Summary

The first setup command in `CONTRIBUTING.md` is `make build-agents`. Its
recipe is `cd agents && $(PIP) install -c … -e ".[dev]"`, and the
Makefile sets `PIP := pip3` separately from `PYTHON := python3`.

So the documented first step installs the agents package and every
development dependency into whatever `pip3` is first on the path.

## Context

- `CONTRIBUTING.md` and `README.md` never mention a virtual environment.
  `.venv` does appear in `CLAUDE.md`, the methodology's automation
  catalogue, the manual-test guides and the release documents, and none
  of them says how to create it.
- `make build-agents PYTHON=.venv/bin/python` still installs through
  system `pip3`, because the recipe uses `$(PIP)`, not
  `$(PYTHON) -m pip`.
- Not run: Homebrew, Debian and Ubuntu Pythons mark themselves as
  externally managed and refuse a bare `pip3 install`. On those the
  Quick Start would stop at its first command. Trying it on a clean
  machine would confirm it.

## Impact

A new contributor either changes their system Python or is stopped at
step one, and nothing in the document says what to do next.

## Proposed fix / investigation path

Add a step to the Quick Start that creates and activates `.venv`. Change
the recipe to `$(PYTHON) -m pip` so one variable picks the interpreter
and its installer.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading the two files. The install was not
> run.
