---
id: ISSUE-0240
summary: "When the wallet denies the model call of a workflow step served by a persona, `PersonaAgent.handle` raises and the task servicer answers FAILED with `Internal error`. A task agent in the same state answers FAILED with `budget exceeded (scope): …` and `error_type: budget_exceeded` in its metadata, so only for a task agent can the orchestrator tell a budget denial from a crash."
status: open
severity: low
area: agents/persona
created: 2026-11-12
refs:
  - agents/persona.py
  - agents/base.py
  - agents/server_servicers.py
  - agents/persona_runtime/llm_call_errors.py
  - docs/issues/ISSUE-0199-persona-failures-travel-as-completed-work.md
---

# ISSUE-0240: A wallet denial on a persona's workflow step fails as `Internal error`

## Summary

The persona's action loop re-raises a wallet denial so that its caller
can render it (`agents/persona_runtime/llm_call_errors.py`). The channel
path does. `PersonaAgent.handle` (`agents/persona.py`) does not catch it,
so it reaches the task servicer's last `except`
(`agents/server_servicers.py`), which logs it and answers FAILED with the
fixed text `Internal error`.

## Context

Reproduced on `main` at `0f3eb3ac` with a model double that raises
`BudgetExceededError`:

| Agent | Result of `handle` |
|---|---|
| Persona | raises `BudgetExceededError` |
| Task agent | `FAILED`, `budget exceeded (per_agent): per_agent budget exceeded for … (spent=… limit=…)`, metadata `error_type: budget_exceeded` |

`BaseAgent._run_llm_loop` (`agents/base.py`) catches the denial and
builds that result, with `budget_scope` and `budget_reason` beside the
`error_type`.

## Impact

The step fails either way, so no workflow goes on past it. What is lost
is the reason: the orchestrator and the operator see `Internal error`
for a persona where they would see a budget denial for a task agent.

## Proposed fix / investigation path

Catch `BudgetExceededError` in `PersonaAgent.handle` and return the
result `_run_llm_loop` returns for it. The fix for
[ISSUE-0199](ISSUE-0199-persona-failures-travel-as-completed-work.md)
made the same parity for the persona's own failures and left the wallet's
alone, because its table did not list it.

## Notes

> 2026-11-12 — found while fixing
> [ISSUE-0199](ISSUE-0199-persona-failures-travel-as-completed-work.md),
> by running each failure through both kinds of agent.
