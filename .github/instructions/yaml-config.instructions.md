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
- **Workflow steps:** DAG structure via `depends_on`. Use `{{ variable }}` for Jinja2-like templating in `input` and `condition` fields.
- **Template references:** `extends: "templates/personas.yaml#anchor"` to inherit persona/sub-agent definitions.
- **Environment overrides:** `config/environments/{development,staging,production}.yaml` for env-specific settings.
- **Optimization profiles:** `cost_optimized`, `speed_optimized`, `quality_optimized`, `simulation_optimized` in `config/optimization.yaml`.
