# RFC 0005 Persona Runtime Flow

This sequence describes how persona agents process events and autonomous ticks.

```mermaid
sequenceDiagram
    participant D as EventDispatcher (dispatch.py)
    participant P as _LLMPersonaAgent (persona_runtime.py)
    participant L as LLMClient (llm_client.py)
    participant E as ActionExecutor (dispatch.py)
    participant M as Memory Stores (memory/*)

    D->>P: on_event(event)
    P->>M: recall related context
    P->>P: inject persona state and behavior prompt
    P->>L: complete(system + context + event)
    L-->>P: actions JSON
    P-->>D: list[AgentAction]
    D->>E: execute(actions)
    E->>M: persist outcomes / notes updates

    loop TickScheduler interval (tick.py)
        P->>P: on_tick()
        P->>M: recall for goal progress
        P->>L: complete(system + context + tick prompt)
        L-->>P: actions JSON
        P->>E: execute(actions)
    end
```

Key points:
- Event-driven execution and tick-driven execution both converge on action execution.
- Memory context is injected before each LLM decision step.
- The tick loop intentionally bypasses `EventDispatcher` — `TickScheduler` calls `ActionExecutor`
  directly after receiving actions from `on_tick()`. This is by design: ticks are autonomous
  self-initiated cycles, not inbound events. Verified against `agents/tick.py`. (F-69-05)
