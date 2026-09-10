---
id: ISSUE-0147
summary: "An agent's `tools` list can name tools nothing registers, and the agent drops them with no log line — so `mcp:github` silently gives code-writer, code-reviewer and ember-owl no GitHub tool (the MCP bridge is a placeholder), and the startup warning RFC 0004 promised was never written; proposes one WARNING per agent at startup naming each unregistered tool"
status: open
severity: low
area: agents/tools
created: 2026-09-10
refs:
  - docs/rfcs/0004-python-agent-grpc-server.md
  - config/agents.yaml
  - agents/base.py
  - agents/persona_runtime/action_loop.py
  - agents/server_persona.py
  - agents/server.py
  - agents/tools/registry.py
  - agents/tools/mcp_bridge.py
---

## Summary

Each agent in `config/agents.yaml` lists the tools it may use. When a name on
that list matches no registered tool, the agent leaves it out without a word:
the model is never offered the tool, and nothing is logged. Today this hides a
real gap. Three agents list `mcp:github` and get no GitHub tool from it,
because the [MCP bridge](../ai-glossary.md#mcp-bridge) that would supply one is
still a placeholder.

## Context

**Where the names are dropped.** Before an agent calls the model, it builds the
list of tools to offer. It keeps only the names the tool registry
(`agents/tools/registry.py`) knows and skips the rest, in two places:

- task agents (`TaskAgent`): `_build_tool_definitions` in `agents/base.py`;
- persona agents (`_LLMPersonaAgent`, the class the agent server builds for
  them): the method of the same name in `agents/persona_runtime/action_loop.py`.

Neither logs a skipped name, and nothing else checks the list: the agent schema
(`schemas/agent.schema.json`) accepts any string, and the Go orchestrator does
not read the list at all.

**What gets dropped today.** A probe on 2026-09-10 (at `f52e7ff6`) loaded every
agent in the shipped `config/agents.yaml` through `load_agent`, with the model
provider mocked and no network, and asked each agent for its tool list twice:

| Agent | Kind | Listed in config | Offered to the model |
|---|---|---|---|
| code-writer | task | `file_read`, `file_write`, `shell_exec`, `mcp:github` | `file_read`, `file_write`, `shell_exec` |
| code-reviewer | task | `file_read`, `mcp:github` | `file_read` |
| ember-owl | persona | `file_read`, `mcp:github` | `file_read` and the four memory tools |

Across all six agents, no log record mentioned a tool or MCP, and none was at
WARNING or above. `get_tool("mcp:github")` returns `None`.

**What was promised.** [RFC 0004](../rfcs/0004-python-agent-grpc-server.md#non-goals)
says under Non-Goals that these entries "produce a warning at startup ('MCP
tools not yet available')". That text appears nowhere in the code.
[#900](https://github.com/mkhomutov/Persatrix/pull/900) records the gap as
divergence D1 on the RFC and leaves the warning out, because adding it changes
behaviour and needs its own proposal. This issue is that proposal.

## Impact

- **The config says one thing and the agents do another.** Someone asking why
  code-reviewer never looked at a pull request finds nothing in the logs.
- **Typos fail the same way.** `file_raed` instead of `file_read` also vanishes
  without a trace. MCP is where the gap shows today; the gap is not about MCP.
- **It will outlive the placeholder.** Once the MCP bridge ships, a misspelled
  or unconfigured `mcp:` entry would fail just as quietly.
- **Low severity.** Nothing crashes, and nothing is granted that should be
  denied. What is lost is a capability the config claims, and nobody is told.

## Proposed fix / investigation path

**1. One warning per agent, at startup.** It names every listed tool that
nothing registers. If any of those names start with `mcp:`, it adds that they
need the MCP bridge, which is planned but not built. For code-reviewer:

```text
Agent 'code-reviewer' lists tools that nothing registers, so it will not be offered them: mcp:github. Tools whose names start with 'mcp:' need the MCP bridge, which is planned but not built yet.
```

Nothing else changes: the agent still starts, and the names are still dropped.
Refusing to start, as `load_agent` does when `model` is missing, would be
stricter, but it would stop three shipped agents from starting until the bridge
exists or their config is edited. A warning matches the harm.

**2. Not in `_build_tool_definitions`.** Both copies run each time the agent
handles a task or an event, so a warning there would repeat on every turn.
Both files are also at the 500-line cap.

**3. Not in `load_agent` either.** It is the one place both agent kinds are
built (`agents/server_persona.py`), but that file is at the cap too, and it
runs before every tool exists. The verbatim recall tool,
`recall_channel_messages`, is registered later, by `wire_recall_tools` in
`AgentServer.start()`. A check in `load_agent` would wrongly flag a persona
that lists it.

**4. Where it should fire: `AgentServer.start()` in `agents/server.py`, just
after the log shipper is installed** (`set_active_shipper`; the shipper
forwards agent logs to the orchestrator). By then every tool is registered:
built-in tools when `agents/tools/builtin.py` is imported, persona memory tools
inside `load_agent`, and the recall tool in `wire_recall_tools`. Firing after
the shipper also puts the warning in `persatrix logs`, not only in the local
log, because records logged before a shipper is installed stay local
(`_ship_to_orchestrator` in `agents/observability/logging.py`). Loop over every
hosted agent. `server.py` has room (469 lines); keep the check itself in a
small new module under `agents/tools/`, so `server.py` gains only the call.

**5. Tests first.** Per the project's test-first rule, a failing pytest in
`tests/unit/python/` comes before any code. The model provider is mocked at the
`LLMClient` boundary, nothing touches the network, and the server starts the
way `test_server_lifecycle.py` starts it (port 0, `_self_register` patched):

- a task agent listing `file_read` and `mcp:github` → exactly one WARNING,
  naming `mcp:github` and the MCP bridge;
- a typo such as `file_raed` → named, without the MCP sentence;
- every name registered, or an empty list → no warning;
- a persona listing `recall_channel_messages` → no warning (pins point 3);
- handling later tasks or events → no further warning (pins "once").

**Out of scope:** building the MCP bridge; checking tool names in
`make validate` (the schema cannot see the tool registry); the per-task
`allowed_tools` filter that RFC 0004 also defers. When the bridge lands, the
`mcp:` sentence should say which server failed; that wording belongs to the
bridge work.

**Slot: proposed v0.4.0.**

- **Not v0.3.16.** That release is in Phase 1, and its
  [scope locks](../v0.3.16-scope-locks.md) hold for the whole cycle; changing
  them takes a dated plan amendment, not a PR. An amendment could fold this in,
  since the change is small, touches no stored data and blocks nothing, but
  that is a scope decision to make openly.
- **v0.4.0** is the next ratified version, and the ROADMAP already places the
  MCP bridge there ([Planned Components (v0.4.0)](../../ROADMAP.md#planned-components-v040)).
  The warning does not depend on the bridge and needs no RFC, so it can be an
  early v0.4.0 PR.
- **[Version-train gate](../methodology/process-glossary.md#version-train-gate):**
  an implementation PR slotted for v0.4.0 can be written and reviewed now, but
  it does not merge until v0.3.16 is tagged. The v0.4.0 plan opening confirms
  or moves the slot with a dated note here.

## Notes

> 2026-09-10 — filed as the proposal that
> [#900](https://github.com/mkhomutov/Persatrix/pull/900) defers. The probe in
> Context was re-run through `load_agent` on the shipped config rather than
> taken from #900. Proposed slot v0.4.0; not v0.3.16 scope. Once #900 merges,
> D1's resolution on RFC 0004 can link here; not done in this change, because
> that section is new in #900.
