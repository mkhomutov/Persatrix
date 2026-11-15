---
applyTo: "config/**/*.yaml,templates/**/*.yaml,blueprints/**/*.yaml,workflows/**/*.yaml"
description: "YAML config conventions: JSON Schema validation, agent ID patterns, deny-by-default permissions, Jinja2 templating"
---

# YAML Configuration

- All config YAML validates against JSON schemas in `schemas/`. Run `make validate` after any edit.
- **Agent IDs:** pattern `^[a-z0-9][a-z0-9-]*[a-z0-9]$` (lowercase + hyphens only).
- **Persona naming:** avoid human-like names for persona IDs/display names; use nickname-style values (for example `ember-owl`).
- **Nickname generator:** `make generate-persona-nickname COUNT=5` (or `python scripts/persona_nickname_generator.py`).
- **Permissions:** deny-by-default. Explicitly whitelist filesystem paths, network domains, and shell commands.
- **Workflow steps:** DAG structure via `depends_on`. Use `{{ variable }}` for Jinja2-like templating in `input`. Every step runs: `condition`, `approval_required` and `approval_timeout` pass the schema and are then ignored ([ISSUE-0202](../../docs/issues/ISSUE-0202-workflow-condition-and-approval-validate-then-do-nothing.md)), so leave them out of a new workflow.
- **Persona templates:** write each persona out in full in `config/agents.yaml`. `extends: "templates/personas.yaml#anchor"` is not built: nothing reads `templates/personas.yaml`, and `make validate` refuses the key ([ISSUE-0267](../../docs/issues/ISSUE-0267-extends-template-references-are-documented-and-not-built.md)).
- **Environment overrides:** `config/environments/{development,staging,production}.yaml` for env-specific settings.
- **Optimization profiles:** `cost_optimized`, `speed_optimized`, `quality_optimized`, `simulation_optimized` in `config/optimization.yaml`.
- **Comments in plain English.** Write `#` comments a non-programmer could follow — say what the setting does and why it matters, briefly. Full rules: [Documentation Guide § Writing Style](../../docs/documentation-guide.md#writing-style).
