# Architecture Diagrams

System-level diagrams for Persatrix. Each file embeds Mermaid source inside a
fenced `mermaid` code block and renders natively on GitHub, in VS Code with
Mermaid preview, or in any Mermaid-aware viewer.

These diagrams describe the **current state of the whole system** (v0.1
workflow surface + v0.2 persona, memory, and cost additions). They are not
tied to a specific RFC — when a phase lands, update the diagrams in place
rather than adding phase-prefixed copies.

## Index

| Diagram | Scope |
|---------|-------|
| [system-overview.md](system-overview.md) | Top-level runtime context: CLI ↔ Orchestrator ↔ Agents, external LLM providers, MCP servers, OTEL, SQLite |
| [component-architecture.md](component-architecture.md) | Package-level layout across Rust, Go, and Python; shipped modules vs stubs reserved for later phases |
| [workflow-execution.md](workflow-execution.md) | End-to-end workflow sequence: CLI → REST → planner → scheduler → executor → agent → LLM, with cost/budget accounting |
| [persona-runtime.md](persona-runtime.md) | Persona agent lifecycle: event-driven dispatch and autonomous tick loop, lock protocol, action-loop termination |
| [memory-architecture.md](memory-architecture.md) | Four memory tiers (working, episodic, relationship, notes), SQLite persistence, context assembly order |

## Editing conventions

- Keep each diagram to one logical concern — split rather than grow.
- Prefer descriptive node labels over terse identifiers. Labels with `<br/>`
  line breaks render cleanly in Mermaid.
- When a diagram references a module, use the module's real path
  (`internal/scheduler`, `agents/persona_runtime/`) so readers can jump from
  the diagram to the code.
- Cross-link related diagrams at the bottom of each file rather than
  duplicating context.

## Regeneration

Mermaid source lives inline — there is nothing to regenerate. CI does not
validate diagram rendering. If you add a new diagram, link it from this
index and from the relevant prose documents
([README.md](../../README.md#documentation),
[docs/guides/persona-agents.md](../guides/persona-agents.md)).
