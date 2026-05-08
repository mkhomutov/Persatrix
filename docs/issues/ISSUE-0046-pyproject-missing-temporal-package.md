---
id: ISSUE-0046
summary: "agents/pyproject.toml [tool.setuptools].packages omits persatrix_agents.temporal — every agent container crash-loops with ModuleNotFoundError"
status: resolved
severity: high
area: build/packaging
created: 2026-05-08
closed: 2026-05-08
refs:
  - agents/pyproject.toml
  - agents/temporal/__init__.py
  - agents/persona_runtime/memory_context.py
  - docs/rfcs/0021-pr-plan.md
---

## Summary

The explicit `[tool.setuptools].packages` list in
[agents/pyproject.toml](../../agents/pyproject.toml) does not include
`persatrix_agents.temporal`. As a result, the package is not copied
into the wheel built by `pip install .` (which is what
`Dockerfile.agent` does), and every agent container — `agent-planner`,
`agent-coder`, `agent-reviewer`, **and the persona `agent-ember-owl`** —
crash-loops on startup with:

```
ModuleNotFoundError: No module named 'persatrix_agents.temporal'
  File ".../persatrix_agents/persona_runtime/memory_context.py", line 21
    from ..temporal.rendering import (...)
```

`docker compose up -d` succeeds, but `docker compose ps` shows all
four agent services in `Restarting (1)` indefinitely.

## Context

`agents/temporal/` (containing `rendering.py` and `__init__.py`) was
introduced in PR #256 / PR #260 as part of RFC 0021 P1 (persona
temporal awareness — Clock seam + now-anchor / recency rendering).
The agent runtime imports it from `agents/persona_runtime/memory_context.py`
and `agents/clock.py`.

`pyproject.toml` uses an **explicit** package list because of the
`agents/ → persatrix_agents` directory remap (auto-discovery cannot
resolve it — see the maintainer note above the list, line 76-79).
The PRs that added `agents/temporal/` did not append the new
sub-package to that list. Editable installs and source-tree pytest
runs read directly from the working directory, so the gap was
invisible locally; only a fresh `pip install .` (i.e. the Docker
build) actually omits the files.

## Impact

- **Docker stack is non-functional** for every agent service since
  PR #256 merged (2026-04-2x). MT-CHAT-001 / MT-PERSONA-001 cannot
  run against the compose deployment because `agent-ember-owl`
  never registers. The orchestrator stays healthy but has zero
  agents to dispatch to — every chat or workflow request returns
  404 / "no agents".
- The maintainer note at `agents/pyproject.toml:76-79` predicted
  exactly this failure mode ("imports will silently fail in
  editable installs" — actually only fails in *non-editable*
  installs, but the warning was directionally correct).
- No CI signal: there is no Docker-build smoke test that imports
  `persatrix_agents` from the published wheel.

## Fix

Add `"persatrix_agents.temporal"` to the explicit `packages` list in
`[tool.setuptools]`. One-line change.

```toml
packages = [
    "persatrix_agents",
    "persatrix_agents.generated",
    "persatrix_agents.memory",
    "persatrix_agents.observability",
    "persatrix_agents.persona_runtime",
    "persatrix_agents.sub_agents",
    "persatrix_agents.temporal",   # ← add
    "persatrix_agents.tests",
    "persatrix_agents.tools",
]
```

Verified locally:

1. `docker compose build agent-ember-owl` rebuilds the image with
   the patched wheel.
2. `docker compose up -d` brings all four agent services up
   `(healthy)`.
3. `curl -s http://localhost:8080/api/v1/agents/ember-owl/chat`
   round-trips a real LLM reply (MT-CHAT-001 Step 1 passes).

## Notes

> 2026-05-08 — captured during a fresh `docker compose build && up`
> rehearsal of MT-CHAT-001 against `ember-owl`. All four agent
> containers in `Restarting (1)` until the package list was patched.
> Closing in the same PR that lands the fix; no separate doc PR
> needed.

## Follow-ups (not in this PR)

- A CI step that imports `persatrix_agents` (and a representative
  sub-module) from the freshly built wheel would catch any future
  miss of this kind. Candidate: a `docker run --rm
  persatrix-agent-ember-owl python -c "import
  persatrix_agents.temporal.rendering"` smoke step appended to
  the existing build job.
