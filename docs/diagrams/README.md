# Architecture Diagrams

System-level diagrams for Persatrix. Each file embeds Mermaid source inside a
fenced `mermaid` code block and renders natively on GitHub, in VS Code with
Mermaid preview, or in any Mermaid-aware viewer.

These diagrams describe the **current state of the whole system**, not one
release; [Phase ownership](component-architecture.md#phase-ownership) lists
the release that added each package. They are not tied to a specific RFC —
when a phase lands, update the diagrams in place rather than adding
phase-prefixed copies. No check compares the diagrams with the code, so when a
Go package, CLI command or gRPC service ships, draw it or say in the diagram
why it is left out.

## Index

| Diagram | Scope |
|---------|-------|
| [system-overview.md](system-overview.md) | Top-level runtime context: CLI and web console ↔ Orchestrator ↔ Agents, the wallet's LLM-call leases, accounts and auth, external LLM providers, MCP servers (planned), OTEL, SQLite; includes the human-user chat path, which has run over a DM channel since v0.3.0 (`persatrix chat` or the console → `POST /api/v1/agents/{id}/chat` → channel router → `ReceiveChannelMessage` gRPC) |
| [component-architecture.md](component-architecture.md) | Package-level layout across Rust, Go, Python, and the web console; shipped modules (including the chat surface: `agents/participant.py`, the chat handler in `internal/server/`, the `persatrix chat` CLI command) vs stubs reserved for later phases, plus the packages left out on purpose |
| [workflow-execution.md](workflow-execution.md) | Two sequences: (1) end-to-end workflow run (CLI → REST → planner → scheduler → executor → agent → LLM, with cost/budget accounting); (2) chat-message path (CLI → `POST /chat` → chat executor → `SendChatMessage` gRPC → PersonaAgent → memory → LLM → reply). Sequence 2 still shows chat as it ran before v0.3.0, when it moved onto DM channels |
| [persona-runtime.md](persona-runtime.md) | Persona agent lifecycle: event-driven dispatch and autonomous tick loop, lock protocol, action-loop termination |
| [memory-architecture.md](memory-architecture.md) | Five memory tiers (working, episodic, relationship, notes, facts), SQLite persistence, context assembly order |
| [observability-stack.md](observability-stack.md) | v0.2.3 signal flow: structured-log shipper (agents → `LogService` → ring buffer + disk store → REST/SSE → `persatrix logs` CLI) plus OTLP pipeline (orchestrator + agents → OTEL Collector → Jaeger / Prometheus / Loki), and baggage + trace-context propagation across the gRPC boundary |

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
