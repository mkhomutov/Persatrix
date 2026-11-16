---
description: "Add a new workflow DAG definition. Use when: creating a new automated pipeline, adding a multi-step task flow, defining agent collaboration sequences."
---

# Add Workflow

Create a new workflow called `${{workflow_name}}` that `${{purpose}}`.

## Steps

1. Create `workflows/${{workflow_name}}.yaml` following the pattern in `workflows/feature-builder.yaml`.
2. Define steps as a DAG with `depends_on` edges. Use `{{ variable }}` templating for inputs.
3. Assign each step to an agent defined in `config/agents.yaml`.
4. Do not add `condition`, `approval_required` or `approval_timeout`: the schema accepts them and the scheduler ignores them, so the step would run every time, with no pause ([ISSUE-0202](../../docs/issues/ISSUE-0202-workflow-condition-and-approval-validate-then-do-nothing.md)). `workflows/feature-builder.yaml` carries one such `condition`; do not copy it.
5. Run `make validate` to check against `schemas/workflow.schema.json`.
