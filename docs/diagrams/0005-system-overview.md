# RFC 0005 System Overview

This diagram shows the top-level runtime boundaries and protocols after RFC 0005.

```mermaid
graph LR
    User[User]
    CLI["Rust CLI\ncli/src/main.rs"]
    REST["Orchestrator REST API\ninternal/server"]
    SCHED["Scheduler/Executor\ninternal/scheduler + internal/executor"]
    GRPC["Agent gRPC Services\nagents/server.py"]
    AGENTS["Python Agents\nTaskAgent + PersonaAgent"]
    MEM["Agent Memory\nworking + episodic + relationship"]

    User --> CLI
    CLI -->|HTTP/JSON| REST
    REST --> SCHED
    SCHED -->|gRPC/protobuf| GRPC
    GRPC --> AGENTS
    AGENTS <--> MEM
```

Notes:
- CLI talks only to the orchestrator over REST.
- Orchestrator dispatches work to Python agents over gRPC.
- Memory is owned and managed by the Python agent runtime.
