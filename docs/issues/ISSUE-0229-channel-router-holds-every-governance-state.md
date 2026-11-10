---
id: ISSUE-0229
summary: "`ChannelRouter` is one struct with 59 fields, 15 mutexes and 22 maps, and its 151 methods are spread over 38 files in a package that is 43% of the orchestrator's code. Each governance feature adds another map and lock to it, and the order in which locks may be taken is written only in scattered comments."
status: open
severity: medium
area: internal/channels
created: 2026-11-10
refs:
  - internal/channels/router.go
  - internal/channels/router_autonomous.go
  - internal/channels/interaction_close_latch.go
  - internal/channels/standing_budget.go
  - internal/channels/interaction_resolver.go
  - docs/v0.4.0-plan.md
---

# ISSUE-0229: `ChannelRouter` holds every governance state in one struct

## Summary

Measured on `main`:

| What | Value |
|---|---|
| `ChannelRouter` struct | 247 lines, 59 fields, 15 mutexes, 22 maps |
| Its methods | 151, in 38 files |
| `internal/channels` | 93 production files, 20,750 lines, 43% of the Go production code |

Floor control, reply budgets, end votes, synthesis, convening and
standing schedules each keep their per-channel state as a map on this
one struct, guarded by a lock of their own.

## Context

- The rule for which lock may be taken while holding another is written
  in four comments in four files (`router_autonomous.go`,
  `interaction_close_latch.go`, `standing_budget.go`,
  `interaction_resolver.go`). Nothing checks it.
- A text scan found no method that takes two of these locks in its own
  body, so the rule holds today. Calls from one method into another were
  not traced.
- The files were split to fit the file-length check, not by
  responsibility, so the 38 files still share one namespace and one set
  of fields
  ([ISSUE-0231](ISSUE-0231-size-check-counts-comment-lines.md)).
- v0.4.0 adds society and organization behaviour next to this package.

This is a judgement about cost, not a defect that can be reproduced.

## Impact

A change to one governance feature needs the whole struct in mind to be
safe. Each new feature raises the chance of a lock-order mistake that
shows up only under load.

## Proposed fix / investigation path

In steps, each taken when a change touches the area anyway:

1. Move the 19 `sqlite*` files, which already sit behind the
   `ChannelStore` interface, into their own package.
2. Move the `config*` files likewise.
3. Lift one per-channel state group at a time (reply budget, end vote,
   standing schedule) into a small type that owns its own lock.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; the counts were measured twice.
