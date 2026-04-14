# RFC 0005 Module Structure (Post Refactor)

Python agent package layout after PRs 8a, 8b, 8c, and 8d.

```mermaid
graph TD
    AG[agents/]

    AG --> P0[persona.py]
    AG --> P1[persona_runtime.py]
    AG --> P2[persona_types.py]
    AG --> P3[persona_behavior.py]
    AG --> P4[dispatch.py]
    AG --> P5[tick.py]
    AG --> TA[task_agent.py]
    AG --> SV[server.py]
    AG --> LC[llm_client.py]
    AG --> MEM[memory/]
    AG --> TOOLS[tools/]

    MEM --> M1[working.py]
    MEM --> M2[episodic.py]
    MEM --> M3[notes.py]
    MEM --> M4[relationship.py]
    MEM --> M5[migrations.py]

    TOOLS --> T1[builtin.py]
    TOOLS --> T2[registry.py]
    TOOLS --> T3[permissions.py]
    TOOLS --> T4[sandbox.py]
```

Responsibilities:
- persona.py: PersonaAgent ABC, factory, compatibility re-exports.
- persona_runtime.py: concrete LLM persona runtime implementation.
- dispatch.py and tick.py: event execution and autonomous scheduling.
