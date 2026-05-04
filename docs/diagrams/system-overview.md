# System Overview

Top-level runtime context for Persatrix: which components exist, what they own,
and what external systems they integrate with. This diagram describes the
current state of the whole system — the v0.1 workflow surface, the v0.2
persona/memory/cost additions, and the v0.2.1 human-agent chat surface.

```mermaid
graph LR
    subgraph External["External actors & services"]
        Operator["Operator"]
        HumanUser["Human user"]
        LLM["LLM providers<br/>Anthropic · OpenAI"]
        MCP["MCP servers<br/>stdio / HTTP"]
        OTEL["OTEL collector<br/>Jaeger / Tempo"]
    end

    subgraph Client["Client tier"]
        CLI["Rust CLI<br/>cli/"]
    end

    subgraph Orchestrator["Orchestrator — Go (cmd/orchestrator + internal/)"]
        REST["REST API + SSE<br/>internal/server"]
        PLAN["Planner<br/>internal/planner"]
        SCHED["Scheduler<br/>internal/scheduler"]
        EXEC["Executor<br/>internal/executor"]
        CHATEXEC["Chat executor<br/>internal/executor"]
        REG["Registry<br/>internal/registry"]
        STATE["Run state<br/>internal/state"]
        COST["Cost & budgets<br/>internal/cost"]
        TELE["Telemetry<br/>internal/telemetry"]
    end

    subgraph Agents["Agent runtime — Python (agents/)"]
        AGSVC["gRPC servicer<br/>agents/server.py"]
        TASK["TaskAgent<br/>agents/task_agent.py"]
        PERS["PersonaAgent<br/>agents/persona*"]
        PART["Participant / UserStore<br/>agents/participant.py"]
        TOOLS["Tool registry<br/>agents/tools"]
        MEM["Memory stores<br/>agents/memory"]
    end

    DB[(memory.db<br/>SQLite + FTS5)]

    Operator -->|persatrix run| CLI
    HumanUser -->|persatrix chat| CLI
    CLI -->|HTTP/JSON| REST
    REST --> PLAN
    REST --> STATE
    REST --> COST
    REST -- "POST /api/v1/agents/{id}/chat" --> CHATEXEC
    PLAN --> SCHED
    SCHED --> EXEC
    SCHED --> COST
    EXEC -->|gRPC ExecuteTask| AGSVC
    CHATEXEC -->|gRPC SendChatMessage| AGSVC
    EXEC --> REG
    CHATEXEC --> REG

    AGSVC --> TASK
    AGSVC --> PERS
    AGSVC -. planned .-> PART
    TASK --> TOOLS
    PERS --> TOOLS
    PERS --> MEM
    PART -. planned .-> DB
    MEM --> DB

    TASK -->|HTTPS| LLM
    PERS -->|HTTPS| LLM
    TOOLS -->|stdio/HTTP| MCP
    Orchestrator -.OTEL spans.-> OTEL
    Agents -.OTEL spans.-> OTEL
```

## Boundaries

- **CLI ↔ Orchestrator**: REST + Server-Sent Events over HTTP/JSON. No gRPC
  leaks across this boundary.
- **Orchestrator ↔ Agents**: gRPC/protobuf (`proto/task.proto`). The
  orchestrator never calls LLMs directly.
- **Agents ↔ External**: LLM providers (HTTPS) and MCP servers (stdio or HTTP).
  Both are initiated by the agent runtime, never by the orchestrator.

## Ownership

| Concern | Owner |
|---------|-------|
| Workflow planning, DAG validation, scheduling, retry | Orchestrator (Go) |
| Cost accounting, budget enforcement, response cache | Orchestrator (Go) |
| LLM prompting, tool execution, persona behaviour | Agents (Python) |
| Episodic / relationship / working memory | Agents (Python) |
| Human participant identity, user store | Agents (Python) |
| Chat message routing (REST → gRPC) | Orchestrator (Go) |
| Agent discovery, secrets, gRPC transport | Orchestrator (Go) |
| User-facing commands (workflow run + chat REPL) | CLI (Rust) |

`EXEC` and `CHATEXEC` are both types in the same `internal/executor` Go
package; they are drawn as sibling nodes to make the dual gRPC dispatch
(workflow `ExecuteTask` vs chat `SendChatMessage`) visible.

The `AGSVC -. planned .-> PART` and `PART -. planned .-> DB` edges are drawn
dashed because `agents/participant.py` (`UserParticipant`, `UserStore`) ships
in v0.2.1 but is **not yet invoked from the chat path**. The current
`SendChatMessage` servicer only calls `validate_participant_type` from
`participant.py` and writes relationship memory directly via
`agent.memory.relationship.record_interaction(...)`, keyed on `(agent_id,
user_id)` without going through `UserStore.get_or_create()`. Wiring the
participant store into the chat path is tracked as a follow-up; the diagram
keeps the nodes visible so the architectural intent is preserved.

See [component-architecture.md](component-architecture.md) for the module-level
view of each component.
