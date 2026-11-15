---
id: ISSUE-0267
summary: "The YAML instructions told authors to inherit a persona with `extends: \"templates/personas.yaml#anchor\"`, and `templates/personas.yaml` says the same in its header. Nothing reads that file or resolves `extends`, and `make validate` refuses the key. The instruction line is corrected; the unused template file and its header remain."
status: open
severity: low
area: config
created: 2026-11-15
refs:
  - templates/personas.yaml
  - .github/instructions/yaml-config.instructions.md
  - .github/copilot-instructions.md
  - schemas/agent.schema.json
  - agents/sub_agents/templates.py
---

# ISSUE-0267: `extends:` template references are documented and not built

## Summary

`templates/personas.yaml` holds persona archetypes under
`persona_templates` and opens with the comment
`# Reference: { extends: "templates/personas.yaml#senior_engineer" }`.
The YAML instructions for assistants said the same: use
`extends: "templates/personas.yaml#anchor"` "to inherit persona/sub-agent
definitions".

No code reads `templates/personas.yaml`, and no code looks for an
`extends` key: neither the Go config loader nor the Python agents nor
the validator. The agent schema allows no unknown key, so a persona
that carries `extends` fails `make validate`:

```
agents.yaml at $.agents.3.persona: Additional properties are not allowed ('extends' was unexpected)
```

## Context

- The failure is loud, not silent: an author who follows the old
  instruction finds out at `make validate`.
- The shared assistant instructions list `templates/personas.yaml` in
  their project layout as "Reusable persona archetypes". That describes
  the file's content, not a mechanism, and is left as it is.
- `agents/sub_agents/templates.py` is a stub whose `TODO`s name
  `templates/sub_agents.yaml`, a file that does not exist.

Checked on `main` at `111b041d`: a search of `agents/`, `internal/`,
`cmd/`, `cli/src/`, `scripts/`, `schemas/` and `config/` for `extends`
and for the file's name finds only the file's own comment, and the
validator was run on a copy of the shipped config with the key added.

## Impact

An author, or an assistant following the instructions, writes a config
that does not validate, and the template file suggests a feature that
is not there. Proposed low: nothing runs wrongly. The severity is
proposed here, not ruled.

## Proposed fix / investigation path

Decide whether persona templates are wanted. If so, design the
reference (the schema key, where it is resolved, what a persona may
override) and build it on both sides that read the config. If not,
delete `templates/personas.yaml` and its row in the instructions'
project layout.

## Notes

> 2026-11-15 — found in [#1133](https://github.com/mkhomutov/Persatrix/pull/1133)
> while correcting the instruction files for
> [ISSUE-0202](ISSUE-0202-workflow-condition-and-approval-validate-then-do-nothing.md)
> and
> [ISSUE-0214](ISSUE-0214-declared-tool-permissions-are-never-enforced.md).
> The instruction line now says that `extends` is not built and to write
> each persona out in full.
