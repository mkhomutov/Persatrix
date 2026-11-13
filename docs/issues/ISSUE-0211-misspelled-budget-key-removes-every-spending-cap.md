---
id: ISSUE-0211
summary: "The optimization schema has no `cost` section and the Go loader ignores unknown keys, and a limit of zero means no limit. So a misspelled `budgets` key passes `make validate` and loads all three spending caps as unlimited, with one startup log line as the only sign."
status: resolved
severity: medium
area: cost
created: 2026-11-10
closed: 2026-11-13
refs:
  - internal/cost/config.go
  - cmd/orchestrator/cost.go
  - internal/cost/cost_config_strict_test.go
  - internal/wallet/config_strict_test.go
  - tests/unit/python/test_optimization_cost_schema.py
  - Makefile
  - internal/cost/cost.go
  - internal/wallet/config.go
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

- `agents/validate.py` reported that validation passed.
- `LoadCostConfig` returned a global, a per-workflow and a per-agent
  limit of 0.

Renaming the whole block, `cost:` to `costs:`, passes validation the same
way, because the schema's top level is open too.

The wallet's lease gate uses the same budget enforcer, so every model
call loses its three dollar caps. The wallet's own per-interaction token
ceiling is separate and stays. Channels, auth and UI config are already
decoded with `KnownFields(true)` and refuse an unknown key.

## Impact

One typo in the file an operator edits to control spending turns the
control off. The wrong key is silent at validation and at load.

## Proposed fix / investigation path

- Add a `cost` property to the schema with `additionalProperties: false`
  at each level, and close the schema's top level once it lists every key
  the file really has, so a misspelled `cost` is caught too.
- Decode the cost block strictly. A strict decode of the whole file would
  refuse the sections other loaders own, and `Node.Decode` does not
  honour `KnownFields`, so the block has to go through a strict decoder
  of its own. `internal/wallet/config.go` reads the same file in the same
  lenient way.
- Treat a missing `cost.budgets` as an error, or at least log a warning
  at startup when all three limits are zero.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; reproduced on a scratch copy of the config folder.

> 2026-11-13 — fixed. Checked against the code at `dd006cba` first: both
> renames (`budgets` to `budgetz`, `cost` to `costs`) pass validation and
> load all three limits as 0 there. Two things in the proposed fix did
> not hold as written:
>
> - **The schema's top level cannot be closed.** Besides `active_profile`
>   it holds one block per optimization profile, under names the operator
>   chooses (`agents/optimization.py` reads `cfg[<active_profile>]`), so a
>   closed list would refuse a profile. The schema requires `cost`
>   instead: a misspelled `cost` reads as one more profile, and the
>   missing block gives it away.
> - **The whole file can be decoded strictly after all.** The loader's
>   struct takes every top-level key it does not own in an inline map,
>   which the strict check leaves alone, so only the `cost` block is
>   checked. Error lines are the file's own.
>
> What changed:
>
> - `LoadCostConfig` refuses a key under `cost` it does not know, and a
>   file with no `cost.budgets`. The wallet loader refuses an unknown key
>   under `wallet` the same way.
> - The schema describes `cost`, closed at every level, and requires it
>   and `cost.budgets`.
> - Both options for a missing or empty limit were taken, one each: a
>   missing `cost.budgets` is an error, and limits that are present and
>   all zero (the way to say "no limit" on purpose, which the EXP-001
>   harness does) get a start-up warning.
> - `make validate` runs over each demo folder as well. All eight
>   shipped files pass unchanged.
>
> A refused cost config leaves the orchestrator running with no wallet,
> so agents' model calls fail: that is
> [ISSUE-0204](ISSUE-0204-grpc-server-starts-only-if-the-log-buffer-did.md)'s
> second path, which this fix makes easier to reach and does not change.
