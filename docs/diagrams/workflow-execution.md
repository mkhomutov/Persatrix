# Workflow Execution

End-to-end sequence for a task-style workflow: from CLI submission, through
DAG planning and stage-level scheduling, to per-step gRPC dispatch. This is
the v0.1 surface; v0.2 layered cost accounting and budget enforcement onto
the same path. v0.2.1 added a separate chat-message path, shown in the second
diagram below.

## Workflow execution sequence

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

    User->>CLI: persatrix run workflow.yaml
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

## Chat message sequence (v0.2.1)

```mermaid
sequenceDiagram
    autonumber
    participant Human as Human user
    participant CLI as Rust CLI<br/>(persatrix chat)
    participant Srv as REST Server<br/>(internal/server)
    participant ChatExec as Chat executor<br/>(internal/executor)
    participant Reg as Registry<br/>(internal/registry)
    participant Agent as PersonaAgent<br/>(agents/persona*)
    participant Mem as Memory stores<br/>(agents/memory)
    participant LLM as LLM Provider

    Human->>CLI: persatrix chat <agent_id> [--user <user_id>]
    CLI->>Srv: POST /api/v1/agents/{id}/chat<br/>{ message, user_id, chat_session_id? }
    Srv->>Reg: look up agent endpoint
    Reg-->>Srv: gRPC address
    Srv->>ChatExec: dispatch SendChatMessage
    ChatExec->>Agent: SendChatMessage(ChatRequest) [gRPC]

    Agent->>Mem: load working context + episodic recall
    Mem-->>Agent: context window
    Agent->>LLM: complete(system+context+message)
    LLM-->>Agent: reply text + usage
    Agent->>Mem: store episodic episode (user msg + reply)
    Agent->>Mem: update relationship memory (trust score, interaction count)
    Agent-->>ChatExec: ChatResponse { reply, chat_session_id, reply_status }
    ChatExec-->>Srv: ChatResponse
    Srv-->>CLI: 200 { reply, chat_session_id, agent_display_name }
    CLI-->>Human: print reply

    loop User continues chatting
        Human->>CLI: next message
        Note over CLI,Srv: same chat_session_id re-used
        CLI->>Srv: POST /api/v1/agents/{id}/chat<br/>{ message, user_id, chat_session_id }
    end

    Human->>CLI: exit (or Ctrl-C)
    CLI-->>Human: session ended
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

Retry **policy** lives inside the `Executor` — it owns the backoff loop for
transient gRPC and LLM errors and only surfaces failure to the `Scheduler`
once the attempts are exhausted ([internal/executor/executor.go:369](../../internal/executor/executor.go#L369)
sets `result.RetryCount = attempt`). The `Scheduler` still consumes that count
when it folds step results into cost metadata and span attributes
([internal/scheduler/stage_runner.go:192](../../internal/scheduler/stage_runner.go#L192),
[internal/scheduler/budget.go:236](../../internal/scheduler/budget.go#L236)) —
so the retry is invisible to scheduling, but the *outcome* is not.

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

## What v0.2.1 added on this path

- `POST /api/v1/agents/{id}/chat` REST endpoint in `internal/server/chat_handler.go`.
- `SendChatMessage` gRPC RPC in `proto/task.proto` dispatched by `internal/executor/`.
- `agents/participant.py` — `UserParticipant` and `UserStore` for per-user
  identity persistence and relationship memory keyed on `(agent_id, user_id)`.
- `persatrix chat <agent_id>` CLI command (interactive REPL).

## Known gaps on the chat path

These are intentionally documented in prose rather than the sequence diagram
above so the diagram stays a description of the *runtime* shape, not a list of
open tickets.

- **`UserStore` is not yet invoked from the chat path.** `SendChatMessage` in
  `agents/server_servicers.py` only calls `validate_participant_type` from
  `agents/participant.py`; relationship memory is written via
  `agent.memory.relationship.record_interaction(...)` keyed on `user_id`
  directly, without going through `UserStore.get_or_create()`. The store ships
  in v0.2.1 and is exercised by its own unit tests, but wiring it into the
  servicer is a follow-up. This is also the reason the `AGSVC --> PART` and
  `SRV --> PART` edges are drawn dashed in [system-overview.md](system-overview.md)
  and [component-architecture.md](component-architecture.md).
- **Chat is not on the cost-tracking path.** The workflow sequence above
  records tokens, USD and cache-hits via `internal/cost/` after every
  `ExecuteTask`. The chat path does not: `internal/executor/chat.go` and
  `agents/server_servicers.py` do not reference the cost tracker, and
  `ChatResponse` carries no `usage` field today. Chat token spend is therefore
  invisible to `GET /api/v1/cost/summary`. Closing this gap is tracked
  separately from this diagram refresh.
