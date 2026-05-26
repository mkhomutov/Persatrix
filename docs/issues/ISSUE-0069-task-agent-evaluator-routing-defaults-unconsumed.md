---
id: ISSUE-0069
summary: "model_routing.defaults.task_agents / .evaluators are migrated to aliases and surfaced by model_routing_defaults() but consumed by no runtime path — only sub_agents is wired; an agent with no usable model: is hard-stopped (schema-rejected if absent, SystemExit if empty), never falling back to the routing default"
status: open
severity: low
area: agents/optimization
created: 2026-05-26
refs:
  - docs/rfcs/0033-model-alias-layer.md
  - docs/rfcs/0033-pr-plan.md
---

## Summary

`config/optimization.yaml`'s `default.model_routing.defaults` declares three
roles — `task_agents`, `sub_agents`, `evaluators` — and RFC 0033 PR 3 (#433)
migrated all three from raw vendor IDs to aliases (`quality` / `quality` /
`fast`). The new `agents.optimization.model_routing_defaults()` accessor
surfaces all three. But only `sub_agents` has a runtime consumer
(`sub_agent_default_model()` → `SubAgentRequest.__post_init__`). Nothing reads
`task_agents` or `evaluators`, so config advertises a routing-default
capability that the runtime honours for only one of its three roles.

## Context

Found during the #433 review.

- `agents/optimization.py:model_routing_defaults()` returns the full
  `{task_agents, sub_agents, evaluators}` map.
- `agents/optimization.py:sub_agent_default_model()` reads **only**
  `defaults["sub_agents"]`; it is the lone consumer.
- Task agents take their model from the explicit `model:` field in
  `config/agents.yaml` (now `quality`), resolved by
  `agents/llm_factory.py:create_provider`. There is no read of the
  `task_agents` routing default anywhere. `model` is schema-`required`
  (`agents[].required` in [schemas/agent.schema.json](../../schemas/agent.schema.json)),
  so an agent that **omits** `model:` is rejected by `make validate` before it
  reaches the runtime; if validation is bypassed, `model = agent_config["model"]`
  ([agents/llm_factory.py:170](../../agents/llm_factory.py)) raises `KeyError`, and
  an explicit empty `model: ""` raises
  `SystemExit("Agent config 'model' field is empty")`
  ([agents/llm_factory.py:174](../../agents/llm_factory.py)). None of these paths
  falls back to `defaults.task_agents`.
- No runtime path reads `defaults.evaluators` either.

This pre-dates #433 — the `defaults` block existed with raw vendor IDs and was
equally unconsumed for `task_agents`/`evaluators` then. #433 did not introduce
the gap; it made it more visible by adding the accessor that advertises all
three roles. Hence a follow-up, not a #433 blocker.

## Impact

Low. Config-surface footgun: a maintainer who drops the explicit `model:` from
a task agent — reasonably expecting `defaults.task_agents` to apply, since it is
right there in config and `model_routing_defaults()` returns it — instead hits a
hard stop. `make validate` rejects the agent (`model` is schema-`required`); or,
if an empty `model: ""` slips past validation, `create_provider` `SystemExit`s.
Neither falls back to the routing default. The config surface promises a
fallback that two of its three roles do not implement.

## Proposed fix / investigation path

Pick one (decide intent first):

1. **Wire it.** Mirror the sub-agent `None`-sentinel resolution: when an agent
   config omits `model:`, have `create_provider` (or the task-agent /
   evaluator construction site) fall back to `defaults.task_agents` /
   `defaults.evaluators` via `model_routing_defaults()` before failing loud.
   Makes all three roles behave uniformly.
2. **Document it as reserved.** If task agents and evaluators are intended to
   always carry an explicit `model:`, mark the `task_agents` / `evaluators`
   routing-default keys as reserved/aspirational in the schema + a config
   comment (or drop them) so config does not advertise an unimplemented
   fallback. Keep `sub_agents` as the only live routing default.

## Notes

> 2026-05-26 — initial capture during PR #433 review. RFC 0033 §J.3 specifies
> only the sub-agent `None`-default resolution; it does not state that task
> agents / evaluators consume their routing defaults, so option 2 may be the
> intended design — confirm against RFC §J before wiring.
