---
id: ISSUE-0147
summary: "An agent's `tools` list can name tools nothing registers, and the agent drops them with no log line — so `mcp:github` silently gives code-writer, code-reviewer and ember-owl no GitHub tool (the MCP bridge is a placeholder), and the startup warning RFC 0004 promised was never written; proposes one WARNING per agent at startup naming each unregistered tool"
status: open
severity: low
area: agents/tools
created: 2026-09-10
refs:
  - docs/rfcs/0004-python-agent-grpc-server.md
  - docs/ai-agents-orchestration-spec.md
  - config/agents.yaml
  - agents/base.py
  - agents/persona_runtime/action_loop.py
  - agents/server_persona.py
  - agents/server.py
  - agents/tools/registry.py
  - agents/tools/recall.py
  - agents/tools/mcp_bridge.py
  - internal/observability/logbuffer/buffer.go
  - tests/unit/python/conftest.py
  - scripts/checks/prompt_refs.py
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
  A persona is also offered its memory tools and the verbatim recall tool,
  which it keeps in a list of its own, whatever its `tools` list says.

Neither logs a skipped name, and nothing else checks the list: the agent schema
(`schemas/agent.schema.json`) accepts any string, and the Go orchestrator does
not read the list at all.

The two copies already differ in a related way. When the model asks to run a
tool, a persona runs it only if the tool is on its list, but a task agent runs
any registered tool the model names (`_execute_tools` in `agents/base.py`).

**What gets dropped today.** A probe on 2026-09-10 (at `f52e7ff6`) loaded every
agent in the shipped `config/agents.yaml` through `load_agent`, with the model
provider mocked and no network, and asked each agent for its tool list twice:

| Agent | Kind | Listed in config | Offered after `load_agent` |
|---|---|---|---|
| code-writer | task | `file_read`, `file_write`, `shell_exec`, `mcp:github` | `file_read`, `file_write`, `shell_exec` |
| code-reviewer | task | `file_read`, `mcp:github` | `file_read` |
| ember-owl | persona | `file_read`, `mcp:github` | `file_read` and the four memory tools |

Once the server starts, `AgentServer.start()` also gives every persona the
recall tool, `recall_channel_messages`, so a running ember-owl is offered six
tools. Across all six agents, no log record mentioned a tool or MCP, and none
was at WARNING or above. `get_tool("mcp:github")` returns `None`.

**A related gap, not this issue.** A tool can also be offered and then refused
every time. All three shipped personas list `file_read`, but none has a
filesystem permission, so every call fails with "Permission denied:
filesystem:read". Only the model sees that answer; the permission check logs
the refusal at DEBUG. The warning proposed here would not mention it.

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

**1. One warning per agent, at startup.** It names every entry on the agent's
`tools` list that gives the agent no tool. If any of those entries start with
`mcp:`, it adds that they need the MCP bridge, which is planned but not built.
For code-reviewer:

```text
Agent 'code-reviewer' lists tools it will not be offered, because nothing provides them: mcp:github. Entries that start with 'mcp:' need the MCP bridge, which is planned but not built yet.
```

Nothing else changes: the agent still starts, and the entries are still
dropped. Refusing to start would be stricter, and it is how the repo already
treats other names in agent config that do not resolve: a missing `model` and
a mistyped model alias both stop the agent at startup with a clear error. A
split rule is the strict option to weigh when the fix is written: refuse to
start on an unknown name without the `mcp:` prefix, such as `file_raed`, and
only warn on `mcp:` entries until the MCP bridge exists. It would stop none of
the shipped agents, because every entry they list that gives no tool starts
with `mcp:`. This issue proposes the warning for both; the implementation PR,
or the v0.4.0 plan opening, can choose the split rule instead.

**2. How to tell that an entry gives no tool.** Ask the code that builds the
offered list, rather than writing a third copy of its rule: an entry gives no
tool when `_build_tool_definitions` offers no tool of that name. That is also
how the probe above measured it. A check against the tool registry alone
would pass persona memory tools and the recall tool only because they happen
to land in the registry too; [RFC 0005](../rfcs/0005-persona-agent-memory.md)
says memory tools belong only in the agent's own list. Better still, move the
rule into one small function in `agents/tools/` that turns a `tools` list into
the tools it gives and the entries that give none, and have both
`_build_tool_definitions` copies and the warning call it. That removes the
drift between the copies, shrinks both at-cap files rather than growing them,
and gives the MCP bridge one place to extend. v0.3.16's size-cap sweep plans
to split `agents/base.py` (PRs D3…, cuttable), and this function is a natural
seam for that split.

**3. `mcp:` entries are not tool names.** The
[orchestration spec](../ai-agents-orchestration-spec.md#52-mcp-model-context-protocol-support)
defines `mcp:github` as all allowed tools from the GitHub MCP server, and
`mcp:custom-api/search` as one tool from one server; the tools themselves have
names like `mcp:github/create_pull_request`. No tool will ever be registered
under the name `mcp:github`. Today every `mcp:` entry gives no tool and is
named in the warning. When the MCP bridge lands, it must teach the shared
function to expand `mcp:` entries, so the matching rule for these entries
changes, not only the warning's wording.

**4. Not in `_build_tool_definitions`, and not in `load_agent`.** Both
`_build_tool_definitions` copies run each time the agent handles a task or an
event, so a warning there would repeat on every turn. `load_agent`
(`agents/server_persona.py`) is where the agent server builds both agent
kinds, but it runs before every tool exists: the recall tool,
`recall_channel_messages`, is added later, by `wire_recall_tools` in
`AgentServer.start()`. A check in `load_agent` would wrongly flag a persona
that lists it. The eval harness (`evaluators/persona_driver.py`) builds
personas without the agent server, so it sees no warning from either place;
for a development tool, that is acceptable.

**5. Where it should fire: the end of `AgentServer.start()` in
`agents/server.py`.** By then every tool exists: built-in tools are registered
when `agents/tools/builtin.py` is imported, persona memory tools inside
`load_agent`, and the recall tool in `wire_recall_tools`. Fire it last, after
the agent has registered with the orchestrator and the re-registration watcher
is armed, and wrap it so that any error is logged and swallowed: a warning must
never stop an agent from starting or registering. It must also cope with a
`tools:` key that has no value (it loads as `None`) and with entries that are
not strings. Loop over every hosted agent, and keep the check in a small new
module under `agents/tools/`, so `server.py` gains only the call.

The warning appears in the agent's own log output, which is what
`docker compose logs` shows. It will not appear in `persatrix logs`. The agent
does ship the record to the orchestrator, but the orchestrator keeps only
records that carry an execution ID (`DropNoExecID` in
`internal/observability/logbuffer/buffer.go`), and nothing logged at startup
has one. Showing startup warnings in `persatrix logs` is a separate gap.

**6. Tests first.** Per the project's test-first rule, a failing pytest in
`tests/unit/python/` comes before any code. The model provider is mocked at the
`LLMClient` boundary. Two things in the test harness matter:

- `tests/unit/python/conftest.py` empties the tool registry before every test,
  so built-in tools such as `file_read` are not registered inside a test. Each
  test registers the tools it uses, with a stub `@tool` as
  `test_server_load_agent.py` does, or with `importlib.reload(builtin)`.
- A started server logs other warnings (log shipper reconnects, persona
  catch-up, `COST:` notes), so the tests assert only on the new check's own
  logger.

Most cases call the check directly, on stub agents built from a config dict:

- a task agent listing `file_read` and `mcp:github` → exactly one WARNING,
  naming `mcp:github` but not `file_read`, and the MCP bridge;
- a typo such as `file_raed` → named, without the MCP sentence;
- every entry gives a tool, or the list is empty → no warning;
- a `tools:` key with no value, or an entry that is not a string → no error;
- building the tool list again, as each later turn does → no further warning
  (pins "once").

One test goes through `AgentServer.start()` (port 0, with `_self_register` and
`agents.server.replay_for_persona_agents` patched, as
`test_server_catchup_wiring.py` does): a real persona that lists
`recall_channel_messages` → no warning. That pins point 4: the check runs
after `wire_recall_tools`.

**Out of scope:**

- building the MCP bridge; when it lands, it extends the `mcp:` rule
  (point 3), and the `mcp:` sentence should also say which server failed;
- the per-task `allowed_tools` filter that RFC 0004 also defers;
- tools that are offered but always refused (Context); a follow-up could warn
  when a listed tool needs a permission the agent has no entry for;
- a companion check in `make validate`, worth filing next. `make validate`
  already runs one check beyond the schema (`scripts/checks/prompt_refs.py`,
  for missing prompt files), every built-in tool name is fixed in code, and
  `mcp:` entries can be checked against the server ids in
  `config/mcp-servers.yaml`. It would catch a typo such as `file_raed` in CI,
  before it ships; the memory and recall tool names, added at runtime, would
  need listing.

**Slot: proposed v0.4.0.**

- **Not v0.3.16.** That release is in Phase 1, and its scope was ratified by
  the [sequencing amendment of 2026-08-19](../v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train);
  this issue is not in it. Adding it would take a new dated amendment, decided
  openly. It is not a [fold-in](../methodology/process-glossary.md#fold-in)
  either: a fold-in is a small fix already on a release's critical path, and
  this one blocks nothing.
- **v0.4.0** is the next ratified version, and the ROADMAP already places the
  MCP bridge there ([Planned Components (v0.4.0)](../../ROADMAP.md#planned-components-v040)).
  The warning does not need the MCP bridge to ship first, and needs no RFC, so
  it can be an early v0.4.0 PR; the MCP bridge work then extends its `mcp:`
  rule (point 3).
- **[Version-train gate](../methodology/process-glossary.md#version-train-gate):**
  an implementation PR slotted for v0.4.0 can be written and reviewed now, but
  it does not merge until v0.3.16 is tagged. The v0.4.0 plan opening confirms
  or moves the slot with a dated note here.

## Notes

> 2026-09-10 — filed as the proposal that
> [#900](https://github.com/mkhomutov/Persatrix/pull/900) defers. The probe in
> Context was re-run through `load_agent` on the shipped config rather than
> taken from #900. Proposed slot v0.4.0; not v0.3.16 scope. #900's D1 records
> this gap on RFC 0004 as an accepted divergence; with this issue filed, D1's
> resolution should read "planned fix" and link here, in #900 before it merges
> or in a follow-up. Each `mcp:github` entry in `config/agents.yaml` points
> here.
