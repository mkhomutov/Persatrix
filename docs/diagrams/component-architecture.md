# Component Architecture

Package-level view across the three languages. Shipped packages are shown
as solid boxes; intentional TODO stubs reserved for later phases are shown
with dashed borders.

```mermaid
graph TB
    subgraph Rust["Rust CLI — cli/"]
        direction TB
        MAIN["main.rs"]
        CMD["commands/<br/>agent · workflow · logs · validate · chat"]
        TYPES["types.rs"]
        MAIN --> CMD
        CMD --> TYPES
    end

    subgraph Go["Go orchestrator — cmd/orchestrator + internal/"]
        direction TB
        SERVER["server/<br/>REST API + SSE<br/>+ POST /api/v1/agents/{id}/chat"]
        PLANNER["planner/<br/>YAML → DAG"]
        SCHEDULER["scheduler/<br/>stage runner + budget"]
        EXECUTOR["executor/<br/>gRPC ExecuteTask + SendChatMessage"]
        REGISTRY["registry/"]
        STATE["state/"]
        COST["cost/<br/>tokens · cache · reporter"]
        TELE["telemetry/<br/>OTEL"]
        MCPG["mcp/"]
        PROTOS["protocols/"]

        A2A["a2a/ (stub)"]:::stub
        BRIDGES["bridges/ (stub)"]:::stub
        CHAN["channels/ (stub)"]:::stub
        RES["resilience/ (stub)"]:::stub
        SEC["security/ (stub)"]:::stub
        MESH["mesh/ (stub)"]:::stub

        SERVER --> PLANNER
        SERVER --> STATE
        SERVER --> COST
        SERVER -->|chat dispatch| EXECUTOR
        PLANNER --> SCHEDULER
        SCHEDULER --> EXECUTOR
        SCHEDULER --> COST
        EXECUTOR --> REGISTRY
        EXECUTOR --> PROTOS
        EXECUTOR --> MCPG
    end

    subgraph Py["Python agents — agents/ (persatrix_agents)"]
        direction TB
        SRV["server.py<br/>+ server_persona.py<br/>+ server_servicers.py"]
        BASE["base.py"]
        TASK["task_agent.py"]
        PERSONA["persona.py"]
        PART["participant.py<br/>UserParticipant · UserStore"]
        PRUNTIME["persona_runtime/<br/>memory_context · action_loop · state_persistence"]
        DISPATCH["dispatch.py · tick.py"]
        LLM["llm_client.py"]
        SUB["sub_agents/"]

        subgraph MEM["memory/"]
            WORK["working.py"]
            EP["episodic.py<br/>+ episodic_queries.py"]
            REL["relationship.py"]
            NOTES["notes.py"]
            MIG["migrations.py"]
        end

        subgraph TOOLS["tools/"]
            TREG["registry.py"]
            TBI["builtin.py"]
            TPERM["permissions.py"]
            TSB["sandbox.py"]
            TMCP["mcp_bridge.py"]
        end

        SRV --> BASE
        SRV --> PART
        BASE --> TASK
        BASE --> PERSONA
        PERSONA --> PRUNTIME
        PRUNTIME --> DISPATCH
        PRUNTIME --> MEM
        TASK --> LLM
        PRUNTIME --> LLM
        TASK --> TOOLS
        PRUNTIME --> TOOLS
        PERSONA --> SUB
    end

    Rust -.->|REST/JSON| Go
    Go -.->|gRPC| Py

    classDef stub stroke-dasharray: 4 4,fill:#f7f7f7,color:#666
```

## Phase ownership

| Phase | Shipped components |
|-------|--------------------|
| v0.1 | `planner/`, `scheduler/`, `executor/`, `registry/`, `state/`, `server/`, `mcp/`, `protocols/`, `agents/task_agent.py`, `agents/tools/` |
| v0.2 | `cost/`, `telemetry/`, `agents/persona*`, `agents/persona_runtime/`, `agents/memory/`, `agents/sub_agents/` |
| v0.2.1 | `agents/participant.py` (`UserParticipant`, `UserStore`), `internal/server/chat_handler.go` (`POST /api/v1/agents/{id}/chat`), `internal/executor/` chat path (`SendChatMessage` gRPC), `cli/src/commands/chat` (`persatrix chat`) |
| v0.3+ (stubs) | `a2a/`, `bridges/`, `channels/`, `resilience/`, `security/`, `mesh/` |

The stub packages are placeholders with `TODO` comments that compile but do not
implement behaviour. They are intentional — removing them is a policy violation
per [CLAUDE.md](../../.github/CLAUDE.md).

## Package import rules

- **No import cycles** across language boundaries. CLI never imports from
  `internal/`; agents never import from `internal/`.
- **Generated code** (`internal/generated/`, `agents/generated/`) is produced
  from `proto/*.proto` by `make proto` and is never edited directly.
- **Python package path** is `persatrix_agents` (configured via
  `agents/pyproject.toml` `tool.setuptools.package-dir`).
