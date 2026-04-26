# Prompt Organization

> **Last updated**: 2026-04-26

This document describes how Persatrix organizes prompt assets and the rules
for adding or modifying them.

---

## Why this exists

Inline prompts in YAML configs and Python source drift over time:

- They're hard to diff (YAML literal blocks lose markdown affordances).
- They blur the line between configuration and behavior.
- They're easy to duplicate across agents instead of being shared.

Externalizing prompts to a dedicated tree makes the source of truth obvious,
keeps reviews focused, and prepares the codebase for shared safety/persona
snippets that multiple agents will eventually consume.

## Layout

```
prompts/
└── runtime/
    ├── task-agents/      # System prompts for TaskAgent roles
    ├── persona/sections/ # Reserved for persona prompt section files
    └── safety/           # Reserved for safety/injection-defense snippets
```

`prompts/` is reserved for **runtime** prompts loaded by the agent runtime.
It is intentionally separate from [`.github/prompts/`](../.github/prompts/),
which holds Copilot/authoring scaffolds for humans and is never loaded by
the runtime.

## Ownership boundaries

| Location                          | Loaded by                                      | Owner              |
| --------------------------------- | ---------------------------------------------- | ------------------ |
| `prompts/runtime/task-agents/`    | `agents.prompt_loader` via `load_agent`        | Agent runtime team |
| `prompts/runtime/persona/sections/` | (reserved — see migration rules)             | Agent runtime team |
| `prompts/runtime/safety/`         | (reserved — see migration rules)               | Security team      |
| `.github/prompts/`                | Copilot / human authors                        | Docs team          |

## Task-agent instructions

Each task agent in [`config/agents.yaml`](../config/agents.yaml) declares its
system-prompt instructions via either:

- `instructions: |` — inline (legacy, still supported for one-off agents); or
- `instructions_file: "prompts/runtime/task-agents/<id>.md"` — file reference.

The two fields are mutually exclusive; setting both is a load-time error.
File references resolve relative to the repo root and **must** live under
`prompts/`. Anything outside that subtree (including `..` traversal and
absolute paths) is rejected by `agents.prompt_loader.resolve_instructions`.

## Migration rules

When adding or moving a task-agent prompt:

1. Place the markdown file under `prompts/runtime/task-agents/<id>.md`,
   where `<id>` matches the agent's `id` field in `config/agents.yaml`.
2. Reference it from `agents.yaml` as
   `instructions_file: "prompts/runtime/task-agents/<id>.md"`.
3. Run `make validate` to confirm the schema accepts the change.
4. Run the agent-loader tests:
   `python -m pytest tests/unit/python/test_prompt_loader.py
   tests/unit/python/test_server_load_agent.py
   tests/unit/python/test_validate_agent_schema.py -v`.

When adding a persona section or safety snippet, **do not** wire it into the
runtime as part of the same PR — those subsystems still assemble their
prompts inline (see [`agents/persona_runtime/prompt_assembly.py`](../agents/persona_runtime/prompt_assembly.py)).
File new directories under `prompts/runtime/persona/sections/` or
`prompts/runtime/safety/` and propose an RFC for the externalization.

## Backward compatibility

The runtime keeps full support for inline `instructions:` blocks. Existing
agents that have not yet migrated continue to work without changes; the
schema accepts either field for any task agent.
