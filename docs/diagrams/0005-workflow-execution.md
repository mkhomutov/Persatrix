# RFC 0005 Workflow Execution Sequence

End-to-end execution from workflow submission to step completion.

```mermaid
sequenceDiagram
    participant C as CLI
    participant S as REST Server
    participant P as YAMLPlanner
    participant H as WorkflowScheduler
    participant X as GRPCExecutor
    participant A as AgentService
    participant L as LLM

    C->>S: POST /api/v1/workflows/run
    S->>P: parse + validate DAG
    P-->>S: execution plan
    S-->>C: run_id

    loop polling
        H->>X: execute pending stage steps
        X->>A: ExecuteTask(task)
        A->>L: complete(prompt)
        L-->>A: output
        A-->>X: TaskResult
        X-->>H: step result
        H->>S: update run state
    end

    C->>S: GET /api/v1/workflows/{id}/status
    S-->>C: run + step statuses
```

This flow is unchanged by RFC 0005 at the transport boundary; RFC 0005 expands agent internals (persona, memory, autonomy).
