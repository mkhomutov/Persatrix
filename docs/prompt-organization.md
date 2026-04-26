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

The two fields are **mutually exclusive** — setting both is rejected at
both schema-validation time (the task-type clause uses `oneOf`) and
runtime (`agents.prompt_loader` raises `PromptLoadError`).

Persona agents must declare **neither** field: `create_persona_agent`
does not consume them, so the schema rejects them as a
silent-misconfiguration footgun.

File references resolve relative to the repo root and **must** live under
`prompts/`. The constraint is enforced in three layers:

1. JSON Schema `pattern: "^prompts/"` rejects out-of-tree literals at
   validate time.
2. [`scripts/checks/prompt_refs.py`](../scripts/checks/prompt_refs.py)
   (wired into `make validate`) confirms each reference resolves to a
   real file inside the subtree.
3. `agents.prompt_loader.resolve_instructions` re-checks at runtime
   using `Path.resolve()` + `relative_to(prompts_root)`, so symlink
   targets and `..` traversals that slipped past the static checks
   are still rejected — the runtime is the authoritative deny-by-default
   control.

The `repo_root` anchor for resolution defaults to the package's parent
directory (`Path(__file__).parent.parent` from
[`agents/server_persona.py`](../agents/server_persona.py)), independent
of where `--config` points; operators with non-default layouts can
pass an explicit `repo_root` through `load_agent`.

## Safety / behavior snippets

Short fragments loaded by the runtime itself (not via `agents.yaml`) live
under `prompts/runtime/safety/` and load through
[`agents.prompt_loader.load_snippet`](../agents/prompt_loader.py).
Currently shipped:

| File                          | Loaded by                                                                                         |
| ----------------------------- | ------------------------------------------------------------------------------------------------- |
| `user-message-delimiters.md`  | [`agents/persona_runtime/prompt_assembly.py`](../agents/persona_runtime/prompt_assembly.py)       |
| `memory-tool-usage.md`        | [`agents/persona_runtime/prompt_assembly.py`](../agents/persona_runtime/prompt_assembly.py)       |
| `reflection-nudge.md`         | [`agents/tools/builtin.py`](../agents/tools/builtin.py)                                           |
| `episode-summarizer.md`       | [`agents/memory/episodic_retention.py`](../agents/memory/episodic_retention.py)                   |

`load_snippet(name)` enforces the same deny-by-default posture as
`resolve_instructions`: paths must resolve under
`prompts/runtime/safety/`, path separators in `name` are rejected so a
caller cannot escape into sibling subtrees, and a single trailing
newline is stripped on load so the editor's final-newline convention
doesn't drift the bytes the runtime sees from what was previously
inlined in source. Reads are cached because every persona system-prompt
assembly, auto-reflect tick, and episodic summarization hits the loader.

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

When adding a new safety snippet:

1. Place the markdown file under `prompts/runtime/safety/<name>.md`.
2. Call `load_snippet("<name>")` from the runtime call site that
   previously held the inlined string.
3. Add a bytes-identical regression test in
   [`tests/unit/python/test_prompt_loader.py`](../tests/unit/python/test_prompt_loader.py)
   under `TestShippedSnippetsByteIdentity`.

When adding a persona section, **do not** wire it into the runtime as
part of the same PR — `agents/persona_runtime/prompt_assembly.py` still
assembles the bulk of the persona system prompt inline. File new
section files under `prompts/runtime/persona/sections/` and propose an
RFC for the templating syntax (see PR C in the prompt-externalization
follow-up plan).

## Backward compatibility

The runtime keeps full support for inline `instructions:` blocks. Existing
agents that have not yet migrated continue to work without changes; the
schema accepts either field for any task agent.
