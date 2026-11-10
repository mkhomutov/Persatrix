---
id: ISSUE-0211
summary: "The optimization schema has no `cost` section and the Go loader ignores unknown keys, and a limit of zero means no limit. So a misspelled `budgets` key passes `make validate` and loads all three spending caps as unlimited, with one startup log line as the only sign."
status: open
severity: medium
area: cost
created: 2026-11-10
refs:
  - internal/cost/config.go
  - internal/cost/cost.go
  - schemas/optimization.schema.json
  - config/optimization.yaml
  - agents/validate.py
---

# ISSUE-0211: A misspelled budget key passes validation and removes every spending cap

## Summary

Three things combine:

1. `schemas/optimization.schema.json` lists the top-level properties
   `conversation_window`, `memory_budget`, `models`, `schema_version` and
   `wallet`. It has no `cost` property and does not set
   `additionalProperties: false`, so the whole `cost:` block of
   `config/optimization.yaml` is unchecked.
2. `LoadCostConfig` (`internal/cost/config.go`) decodes the file with a
   plain `yaml.Unmarshal`, which drops keys it does not know.
3. In `CheckBudget` (`internal/cost/cost.go`) a limit applies only when it
   is greater than zero.

## Context

On a scratch copy of the config with `budgets:` renamed `budgetz:`:

- `agents/validate.py` printed "Validation passed (8 file(s) checked)".
- `LoadCostConfig` returned a global, a per-workflow and a per-agent
  limit of 0.

The wallet's lease gate uses the same budget enforcer, so every model
call loses its cap. Channels, auth and UI config are already decoded with
`KnownFields(true)` and refuse an unknown key.

## Impact

One typo in the file an operator edits to control spending turns the
control off. The wrong key is silent at validation and at load.

## Proposed fix / investigation path

Add a `cost` property to the schema with `additionalProperties: false` at
each level. Decode the cost block with `KnownFields(true)`. Consider
logging a warning at startup when all three limits are zero.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; reproduced on a scratch copy of the config folder.
