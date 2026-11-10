---
id: ISSUE-0210
summary: "The agent package is installed as `persatrix_agents`, but six production lines import through `agents.`. The installed entry point fails outside a folder that has an `agents/` directory, and inside one, as in the image, most of the runtime loads twice with separate per-request state. Every test imports `agents.`, so no test can see it."
status: open
severity: medium
area: build/packaging
created: 2026-11-10
refs:
  - agents/pyproject.toml
  - agents/persona_runtime/operator_directive.py
  - agents/persona_runtime/session_id.py
  - agents/observability/log_shipper.py
  - Dockerfile.agent
  - docs/issues/ISSUE-0046-pyproject-missing-temporal-package.md
---

# ISSUE-0210: The agent package runs under two import roots

## Summary

`agents/pyproject.toml` maps the `agents/` folder to the import name
`persatrix_agents`, and the image starts
`python -m persatrix_agents.server`. Inside the package, modules import
each other relatively, except for six lines that write `from agents.…`:

- `agents/persona_runtime/operator_directive.py` (two lines)
- `agents/persona_runtime/session_id.py` (two lines)
- `agents/observability/log_shipper.py` (two lines)

Those lines work only when a folder named `agents` is on the import path.

## Context

- From a folder with no `agents/` directory,
  `python -m persatrix_agents.server --help` fails with
  `ModuleNotFoundError: No module named 'agents'`. The declared console
  script `Persatrix-agent` fails the same way.
- The image copies the source to `/app/agents` and runs from `/app`, so
  there the six lines resolve to a second copy. 241 modules were seen
  loaded a second time as `agents.*` beside 259 as `persatrix_agents.*`.
  `agents.session_id._ACTIVE_SESSION_ID` and its twin are then two
  different context variables.
- The measurements were taken with the shared virtualenv, whose
  `persatrix_agents` points at the main checkout; the six lines are the
  same there and here.

Today only constants, two pure functions and one generated module cross
the two roots, so behaviour is right by luck.
[ISSUE-0046](ISSUE-0046-pyproject-missing-temporal-package.md) was an
earlier fault of the same kind: one that only the installed package shows.

## Impact

The next `from agents.x import <something that holds state>` in
production code will read a session, epoch or principal that nothing set,
which is a memory-isolation fault. No unit test can catch it, because
every test imports `agents.` and no CI job starts the installed package
or the image.

## Proposed fix / investigation path

Change the six lines to relative imports. Ban absolute `agents.` imports
inside `agents/` with a ruff banned-import rule, outside the tests. Add
one CI step that runs `python -P -m persatrix_agents.server --help` from
an empty folder.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; the import failure and the double load were measured with
> the installed package.
