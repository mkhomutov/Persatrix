# System Overview

Top-level runtime context for Persatrix: which components exist, what they own,
and what external systems they integrate with. This diagram describes the
current state of the whole system — both the v0.1 workflow surface and the v0.2
persona/memory/cost additions.

```mermaid
graph LR
    subgraph External["External actors & services"]
        User["Operator"]
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
        REG["Registry<br/>internal/registry"]
        STATE["Run state<br/>internal/state"]
        COST["Cost & budgets<br/>internal/cost"]
        TELE["Telemetry<br/>internal/telemetry"]
    end

    subgraph Agents["Agent runtime — Python (agents/)"]
        AGSVC["gRPC servicer<br/>agents/server.py"]
        TASK["TaskAgent<br/>agents/task_agent.py"]
        PERS["PersonaAgent<br/>agents/persona*"]
        TOOLS["Tool registry<br/>agents/tools"]
        MEM["Memory stores<br/>agents/memory"]
    end

    DB[(memory.db<br/>SQLite + FTS5)]

    User -->|orch run| CLI
    CLI -->|HTTP/JSON| REST
    REST --> PLAN
    REST --> STATE
    REST --> COST
    PLAN --> SCHED
    SCHED --> EXEC
    SCHED --> COST
    EXEC -->|gRPC| AGSVC
    EXEC --> REG

    AGSVC --> TASK
    AGSVC --> PERS
    TASK --> TOOLS
    PERS --> TOOLS
    PERS --> MEM
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
- **Orchestrator ↔ Agents**: gRPC/protobuf (`proto/task.proto`,
  `proto/agent_message.proto`). The orchestrator never calls LLMs directly.
- **Agents ↔ External**: LLM providers (HTTPS) and MCP servers (stdio or HTTP).
  Both are initiated by the agent runtime, never by the orchestrator.

## Ownership

| Concern | Owner |
|---------|-------|
| Workflow planning, DAG validation, scheduling, retry | Orchestrator (Go) |
| Cost accounting, budget enforcement, response cache | Orchestrator (Go) |
| LLM prompting, tool execution, persona behaviour | Agents (Python) |
| Episodic / relationship / working memory | Agents (Python) |
| Agent discovery, secrets, gRPC transport | Orchestrator (Go) |
| User-facing commands | CLI (Rust) |

See [component-architecture.md](component-architecture.md) for the module-level
view of each component.
