---
id: ISSUE-0201
summary: "A workflow step's output is stored under its `output_key`, but a template reads it as `steps.<step id>.output`. A workflow that names the step, as the spec's own example does, runs its first steps and then fails on an unresolved reference; nothing checks the reference when the workflow is parsed."
status: open
severity: medium
area: internal/scheduler
created: 2026-11-10
refs:
  - internal/scheduler/stage_runner.go
  - internal/scheduler/context_package.go
  - internal/planner/planner.go
  - internal/planner/testdata/valid_workflow.yaml
  - workflows/feature-builder.yaml
  - internal/scheduler/scheduler_helpers_test.go
  - docs/ai-agents-orchestration-spec.md
  - docs/rfcs/0001-core-orchestration-pipeline.md
  - docs/v0.4.x-sequencing-2026-10-21.md
  - ROADMAP.md
---

# ISSUE-0201: A step's output is stored under `output_key`, not the step id the template names

## Summary

The scheduler saves each step's output in a map keyed by the step's
`output_key` (`internal/scheduler/stage_runner.go`). `ResolveInputs`
(`internal/planner/planner.go`) reads the template `steps.<id>.output`
and looks `<id>` up in that same map. Its own comment and error text call
`<id>` the step id. The two agree only when a workflow author happens to
write the output key where the grammar says step id.

## Context

- Running `internal/planner/testdata/valid_workflow.yaml`, which has the
  shape of the spec's example in §4.2, through the scheduler dispatched
  `plan` and `implement` and then failed with `unresolved step output
  reference: steps.implement.output (step "implement" not in outputs
  map)`.
- `Parse` checks no template reference, so the error appears only at run
  time, after earlier steps have been paid for.
- A step with no `output_key` stores nothing and cannot be referenced.
- The shipped `workflows/feature-builder.yaml` works because its templates
  use the output keys (`steps.code.output` for the step `implement`).
- The records disagree with each other. RFC 0001 rules that
  "implementations must use `Step.ID` as the canonical key". The
  ROADMAP's line for this feature and a scheduler test
  (`scheduler_helpers_test.go`) both use the output key.
- The same mismatch reaches context packaging: candidate ids are output
  keys while `DependsOn` holds step ids (`context_package.go`), so the
  dependency weight never applies when the two differ. This part was
  found by reading, not by a run.

## Impact

A workflow written from the spec fails mid-run and loses the model calls
of the steps that already ran. The error names a step that did run, which
points the author the wrong way.

## Proposed fix / investigation path

- Key outputs by step id and keep `output_key` as an alias. Resolve the
  alias at lookup; a second map entry would double what each agent is
  sent and what context packaging weighs.
- Refuse an `output_key` that equals another step's id.
- In `Parse`, check that every template reference names a step this one
  depends on, directly or through others, so a bad reference is refused
  before anything is dispatched.

The workflow engine is frozen by ruling (x) of the
[v0.4.x sequencing document](../v0.4.x-sequencing-2026-10-21.md), so a
fix for this boards only through its rulings (q) or (u).

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; the failing run was reproduced with a throwaway Go test
> mapped in with `go test -overlay`.
