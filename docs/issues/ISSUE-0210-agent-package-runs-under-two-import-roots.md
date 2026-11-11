---
id: ISSUE-0210
summary: "The agent package is installed as `persatrix_agents`, but seven production lines import through `agents.`. The installed entry point fails outside a folder that has an `agents/` directory, and inside one, as in the image, most of the runtime loads twice with separate per-request state. Every test imports `agents.`, so no test can see it."
status: resolved
severity: medium
area: build/packaging
created: 2026-11-10
closed: 2026-11-11
closed_pr: 1098
refs:
  - agents/pyproject.toml
  - agents/persona_runtime/operator_directive.py
  - agents/persona_runtime/session_id.py
  - agents/observability/log_shipper.py
  - agents/server_cli.py
  - Dockerfile.agent
  - docs/issues/ISSUE-0046-pyproject-missing-temporal-package.md
---

# ISSUE-0210: The agent package runs under two import roots

## Summary

`agents/pyproject.toml` maps the `agents/` folder to the import name
`persatrix_agents`, and the image starts
`python -m persatrix_agents.server`. Inside the package, modules import
each other relatively, except for seven lines that write `from agents.…`:

- `agents/persona_runtime/operator_directive.py` (two lines)
- `agents/persona_runtime/session_id.py` (two lines)
- `agents/observability/log_shipper.py` (two lines)
- `agents/server_cli.py` (one line, inside a function)

Those lines work only when a folder named `agents` is on the import path.

## Context

- From a folder with no `agents/` directory,
  `python -m persatrix_agents.server --help` fails with
  `ModuleNotFoundError: No module named 'agents'`. The declared console
  script `Persatrix-agent` fails the same way.
- The image copies the source to `/app/agents` and runs from `/app`, so
  there the seven lines resolve to a second copy. 241 modules were seen
  loaded a second time as `agents.*` beside 259 as `persatrix_agents.*`.
  `agents.session_id._ACTIVE_SESSION_ID` and its twin are then two
  different context variables.
- The measurements were taken with the shared virtualenv, whose
  `persatrix_agents` points at the main checkout; the seven lines are
  the same there and here.

Six of the seven carry only constants, two pure functions and one
generated module across the two roots, so there behaviour is right by
luck. The seventh already crosses with state. `agents/server_cli.py`
starts metrics through a relative import, and after a failed start it
clears the metrics provider through `agents.observability.metrics`. Under
`persatrix_agents` that is the other copy, whose provider was never set,
so the clean-up does nothing.
[ISSUE-0046](ISSUE-0046-pyproject-missing-temporal-package.md) was an
earlier fault of the same kind: one that only the installed package shows.

## Impact

The next `from agents.x import <something that holds state>` in
production code will read a session, epoch or principal that nothing set,
which is a memory-isolation fault. No unit test can catch it, because
every test imports `agents.` and no CI job starts the installed package
or the image.

## Proposed fix / investigation path

Change the seven lines to relative imports. Ban absolute `agents.`
imports inside `agents/` with a ruff banned-import rule, outside the
tests. The repository-root ruff config extends the agents' one, so the
rule also has to leave `scripts/` and `evaluators/` alone; 16 files
there import `agents.` on purpose. Add
one CI step that runs `python -P -m persatrix_agents.server --help` from
an empty folder.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; the import failure and the double load were measured with
> the installed package.

> 2026-11-11 — fixed. The seven lines are relative imports, a unit test
> (`tests/unit/python/test_agents_package_imports_are_relative.py`)
> refuses a new absolute one, and CI starts the installed package from an
> empty folder. Re-measured first: the seven lines, the failed start and
> the 241 twice-loaded modules all reproduced. The ruff rule proposed
> above does not work: ruff reads `from .x import y` inside `agents/` as
> an import of `agents.x`, so banning `agents` flags every relative
> import in the package. The unit test does that job instead.
