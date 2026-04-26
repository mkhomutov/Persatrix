# Persatrix Prompt Assets

This directory is the source of truth for prompts that the agent runtime
loads at startup. It is intentionally separate from `.github/prompts/`
(which holds Copilot/authoring scaffolds for humans, not runtime prompts).

## Layout

```
prompts/
└── runtime/
    ├── task-agents/      # System prompts for TaskAgent roles
    ├── persona/sections/ # Reserved for persona prompt section files
    └── safety/           # Reserved for safety/injection-defense snippets
```

See [`docs/prompt-organization.md`](../docs/prompt-organization.md) for the
ownership boundaries, migration rules, and how to add new prompt files.
