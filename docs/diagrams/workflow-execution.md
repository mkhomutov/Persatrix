# Workflow Execution

End-to-end sequence for a task-style workflow: from CLI submission, through
DAG planning and stage-level scheduling, to per-step gRPC dispatch. This is
the v0.1 surface; v0.2 layered cost accounting and budget enforcement onto
the same path (highlighted below).

```mermaid
sequenceDiagram
    autonumber
    participant User as Operator
    participant CLI as Rust CLI
    participant Srv as REST Server<br/>(internal/server)
    participant Plan as YAMLPlanner
    participant Sched as Scheduler<br/>(stage_runner)
    participant Exec as Executor<br/>(gRPC)
    participant Agent as Python Agent<br/>(task_agent.py)
    participant LLM as LLM Provider
    participant Cost as Cost tracker<br/>(internal/cost)

    User->>CLI: orch run workflow.yaml
    CLI->>Srv: POST /api/v1/workflows/run
    Srv->>Plan: parse + validate DAG
    Plan->>Plan: cycle detection + topological sort
    Plan-->>Srv: stages[] (parallel-ready sets)
    Srv-->>CLI: run_id (202 Accepted)

    loop For each stage
        Sched->>Cost: check budget (max_tokens, max_llm_calls)
        alt budget exhausted
            Cost-->>Sched: BudgetExceeded
            Sched-->>Srv: mark run failed
        else budget ok
            par Parallel steps in stage
                Sched->>Exec: execute step_i
                Exec->>Agent: ExecuteTask(task) [gRPC]
                Agent->>LLM: complete(prompt, tools)
                LLM-->>Agent: output + usage
                Agent-->>Exec: TaskResult + cost metadata
                Exec->>Cost: record tokens/cost/cache-hit
                Exec-->>Sched: step result
            end
            Sched->>Srv: update run + step state
        end
    end

    CLI->>Srv: GET /api/v1/workflows/{run_id}/status
    Srv-->>CLI: run + per-step status + cost summary
    opt Cost endpoint
        CLI->>Srv: GET /api/v1/cost/summary
        Srv-->>CLI: aggregated tokens · USD · cache hits
    end
```

## Step output templating

Downstream steps reference upstream outputs with Jinja2-like syntax:

```yaml
steps:
  - id: research
    agent: researcher
    input: { topic: "{{ workflow.inputs.topic }}" }
  - id: draft
    agent: writer
    depends_on: [research]
    input: { context: "{{ steps.research.output }}" }
```

The planner resolves these references at stage-entry time, after all
`depends_on` steps in earlier stages have produced output.

## Retry semantics

Retries happen inside the **Executor** (not the Scheduler). An individual step
may retry with backoff on transient gRPC or LLM errors before surfacing failure
to the scheduler. Retry counts are folded into the step's cost metadata.

## What v0.2 added on this path

- `internal/cost/` — tokens/USD/cache-hit accounting and response cache.
- Per-step metadata: `EstimatedCostUSD`, `TokensUsed`, `LLMCallCount`,
  `RetryCount`, `CacheHit`, `WallTimeMs`.
- Pre-stage budget checks in `internal/scheduler/budget.go`. Exceeding
  `max_tokens` or `max_llm_calls` aborts the run with a structured error.
- `GET /api/v1/cost/summary` endpoint for post-hoc inspection.

Cost tracking is orthogonal to the persona runtime — persona agents hit the
same cost tracker when they call `LLMClient.complete()`. See
[persona-runtime.md](persona-runtime.md) for the autonomous/event-driven flow.
