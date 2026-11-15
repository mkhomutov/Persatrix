---
id: ISSUE-0202
summary: "A workflow step's `condition`, `approval_required` and `approval_timeout` keys pass the schema and are then ignored. The shipped `feature-builder` workflow always runs its conditional `revise` step, and a step marked as needing approval runs with no pause and no warning. The instructions, the glossary, the spec and the shipped workflow now say so; the keys are still accepted in silence."
status: in_progress
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
  - .github/prompts/add-workflow.prompt.md
  - docs/ai-glossary.md
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

> 2026-11-15 — the doc half is done in
> [#1133](https://github.com/mkhomutov/Persatrix/pull/1133); the code half
> remains, so this stays `in_progress`. Checked again on `main` at `111b041d` with a throwaway
> Go test that runs the shipped `feature-builder` through the scheduler
> with a reviewer that answers `{"approved": true}`: four steps are
> dispatched, `revise` among them, and the run completes. With
> `approval_required: true` and `approval_timeout: 3600` added to
> `revise` the result is the same, in 32 ms.
>
> What now says that the keys are accepted and ignored: the shared
> assistant instructions and the YAML instructions, which told authors to
> template `condition` fields; the add-workflow prompt, which told them
> to "add conditions and approval gates where needed"; the glossary's
> Workflow entry, which listed conditional and looped steps as
> supported; three dated notes in the orchestration spec (at the
> orchestrator's "conditional" order, at the workflow's conditional
> branching and loops, and at the example's `condition`; the step's
> approval keys already had their note); and a comment at the `revise` step of
> `workflows/feature-builder.yaml`. No behaviour changes: no warning, no
> refusal, nothing in the schema.
>
> What remains is the proposed fix above: refuse the keys at parse, or
> warn at parse and at dispatch. Refusing reverses a choice RFC 0001
> records, so it needs a decision, and either one changes the workflow
> engine, which is frozen. The schema gives the three keys no
> `description`, so there was nothing to correct in it.

> 2026-11-16 — two corrections after the review of [#1133](https://github.com/mkhomutov/Persatrix/pull/1133).
> The note under the spec's workflow example said that its `revise`
> runs on every run. That example does not get that far: it has no
> `schema_version`, and with one added the run fails at `review` on an
> unresolved reference
> ([ISSUE-0201](ISSUE-0201-step-output-stored-under-output-key-not-step-id.md)).
> Only the shipped `workflows/feature-builder.yaml` runs `revise`, on
> every run; the note now says both. And the pull request's title spoke
> of "approval gates" for the step's `approval_required` and
> `approval_timeout` keys. The glossary's Approval Gate is another
> thing, a step of RFC 0028's decision pipeline, and that step exists
> in the code (`agents/decision/pipeline.py`); the title and the
> glossary entry now keep the two apart.
