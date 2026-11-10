---
id: ISSUE-0202
summary: "A workflow step's `condition`, `approval_required` and `approval_timeout` keys pass the schema and are then ignored. The shipped `feature-builder` workflow always runs its conditional `revise` step, and a step marked as needing approval runs with no pause and no warning."
status: open
severity: medium
area: internal/scheduler
created: 2026-11-10
refs:
  - internal/scheduler/stage_runner.go
  - internal/planner/planner.go
  - schemas/workflow.schema.json
  - workflows/feature-builder.yaml
  - docs/ai-agents-orchestration-spec.md
  - docs/rfcs/0001-core-orchestration-pipeline.md
  - docs/rfcs/0003-scheduler-executor.md
  - docs/rfcs/0007-conditional-looped-workflow-control-flow.md
  - docs/v0.4.x-sequencing-2026-10-21.md
  - .github/copilot-instructions.md
  - .github/instructions/yaml-config.instructions.md
---

# ISSUE-0202: Workflow `condition` and `approval_required` validate and then do nothing

## Summary

`schemas/workflow.schema.json` accepts three step keys that the scheduler
never acts on:

- **`condition`.** The planner parses it into `Step.Condition`. No code
  outside a parse test reads that field. `executeStep`
  (`internal/scheduler/stage_runner.go`) carries
  `// TODO(v0.2): evaluate step conditions` and dispatches the step.
- **`approval_required` and `approval_timeout`.** The planner's `Step`
  has no such fields, so both keys are dropped on parse.

## Context

- The shipped `workflows/feature-builder.yaml` gives its `revise` step the
  condition `{{ steps.review.output.approved == false }}`. A run with a
  reviewer output of `{"approved": true}` still dispatched `revise` and
  completed.
- The orchestration spec says of `approval_required: true` that it
  "pauses and asks for human confirmation".
- The shared assistant instructions and the YAML instructions both tell
  authors to template `condition` fields.
- Leaving both out was a recorded choice for v0.1. RFC 0003 lists
  condition evaluation and approval gates under its non-goals, and
  RFC 0001 says unknown keys are "silently ignored" on purpose, "for
  forward compatibility". Both pointed at v0.2, and the docs above were
  never marked.
- [RFC 0007](../rfcs/0007-conditional-looped-workflow-control-flow.md)
  covers building conditions and is not slotted for a release. This issue
  is about the silent acceptance until then.

## Impact

Every run of a workflow with a conditional step pays for that step. An
author who marks a risky step as needing approval gets no pause, and
`make validate` reports the file as valid.

## Proposed fix / investigation path

Until the features exist, refuse a non-empty `condition` and
`approval_required: true` at parse, or log a warning at parse and at
dispatch. Refusing reverses RFC 0001's choice to ignore these keys, so it
needs a decision; a warning does not. Mark the keys as not built in the
two instruction files and in the spec.

The workflow engine is frozen by ruling (x) of the
[v0.4.x sequencing document](../v0.4.x-sequencing-2026-10-21.md), so a
code fix boards only through its rulings (q) or (u). The doc corrections
do not wait for that.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; the `revise` run was reproduced with a throwaway Go test.
