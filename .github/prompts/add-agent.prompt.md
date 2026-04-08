---
description: "Add a new agent type to the framework. Use when: creating a new specialized agent, adding an agent role, extending the agent registry."
---

# Add New Agent

Create a new agent called `${{agent_name}}` with role `${{role}}`.

## Steps

1. Create `agents/${{agent_name}}.py` following the BaseAgent/PersonaAgent pattern in `agents/base.py`.
2. Add the agent definition to `config/agents.yaml` with appropriate permissions (deny-by-default).
3. Register capabilities, tools, and model configuration.
4. Add unit tests in `tests/unit/python/test_${{agent_name}}.py`.
5. Run `make validate` to check config, then `make test-python` to run tests.
