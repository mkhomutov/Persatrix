---
description: "Add a new workflow DAG definition. Use when: creating a new automated pipeline, adding a multi-step task flow, defining agent collaboration sequences."
---

# Add Workflow

Create a new workflow called `${{workflow_name}}` that `${{purpose}}`.

## Steps

1. Create `workflows/${{workflow_name}}.yaml` following the pattern in `workflows/feature-builder.yaml`.
2. Define steps as a DAG with `depends_on` edges. Use `{{ variable }}` templating for inputs.
3. Assign each step to an agent defined in `config/agents.yaml`.
4. Add conditions and approval gates where needed.
5. Run `make validate` to check against `schemas/workflow.schema.json`.
