---
id: ISSUE-0151
summary: "A task agent runs any registered tool the model names, even one missing from its `tools` list: `_execute_tools` in `agents/base.py` never checks the list, while personas refuse such calls — so a valid config whose permissions cover an unlisted tool lets the model use it today, and once ISSUE-0150 lands the shipped planner, code-writer and code-reviewer can each make `http_request` calls they were never offered; proposes that a task agent answer such a call with the persona's `Unknown tool` error, from one rule in `agents/tools/` shared with the offer"
status: open
severity: medium
area: agents/tools
created: 2026-09-11
refs:
  - agents/base.py
  - agents/persona_runtime/action_loop.py
  - agents/tools/registry.py
  - agents/tools/builtin.py
  - agents/tools/memory_tools.py
  - agents/tools/recall.py
  - agents/server_persona.py
  - agents/server_cli.py
  - agents/task_types.py
  - agents/sub_agents/spawner.py
  - config/agents.yaml
  - docs/rfcs/0004-python-agent-grpc-server.md
  - docs/issues/ISSUE-0143-debt-sweep-26-files-at-size-cap.md
  - docs/issues/ISSUE-0147-unregistered-agent-tools-dropped-silently.md
  - tests/unit/python/test_base_handle.py
  - tests/unit/python/test_external_tool_wrapping.py
  - tests/unit/python/test_agents.py
---

## Summary

Each agent in `config/agents.yaml` lists the tools it may use. A persona agent
holds to that list when the model asks to run a tool. A task agent does not: it
runs any registered tool the model names, listed or not. For a task agent the
list only decides which tools the model is told about. Today the permission
gate happens to refuse every tool the shipped task agents leave off their
lists, so nothing is exposed yet. That stops being true for `http_request` once
ISSUE-0150 lands.

## Context

**Two steps, two rules.** Before an agent calls the model, it builds the list
of tools to offer: the registered tools named on its `tools` list
(`_build_tool_definitions`). When the model answers with a tool call, the agent
runs it (`_execute_tools`). Task agents and persona agents each have their own
copy of both methods, and the run step differs:

- a persona agent (`agents/persona_runtime/action_loop.py`) runs a call only if
  the tool is on its list or is one of its own memory or recall tools. Any
  other name gets `Unknown tool: <name>`. The method's docstring says this
  guards against a model naming a tool that is registered but was not offered
  to it;
- a task agent (`agents/base.py`) looks the name up in the whole tool registry
  (`get_tool`) and runs whatever it finds.

Nothing makes a model name only the tools it was offered. It can guess a name,
or follow text it has read — a file, a web page, the task itself — that names
one. For a task agent, the only check left on such a call is the one inside the
tool: each built-in tool asks the permission gate
(`agents/tools/permissions.py`) before it acts.

**What an unlisted call reaches.** A probe on 2026-09-11, at `f0e56ba7`, loaded
agents from a copy of the shipped `config/agents.yaml` through `load_agent`
(`agents/server_persona.py`). It loaded one agent per process, as the agent
server runs them (`agents/server_cli.py` takes a single `--agent`), with the
model provider mocked, outbound HTTP stubbed, and memory and workspace in a
temporary directory. It then called `_execute_tools` with each registered tool
the agent does not list:

| # | Agent (kind) | Config | Unlisted calls | Result |
|---|---|---|---|---|
| 1 | planner (task) | shipped | `file_read`, `file_write`, `shell_exec`, `http_request` | all refused by the permission gate |
| 2 | code-writer (task) | shipped | `http_request` | refused by the permission gate |
| 3 | code-reviewer (task) | shipped | `file_write`, `shell_exec`, `http_request` | all refused by the permission gate |
| 4 | code-reviewer (task) | shipped, plus the `network:http` grant ISSUE-0150 proposes | `http_request` | **ran**: sent `GET https://api.anthropic.com/v1/models`, caught by the stub |
| 5 | code-reviewer (task) | shipped, plus `filesystem.write` over its workspace | `file_write` | **ran**: wrote the file |
| 6 | iron-fox (persona) | shipped | `file_write`, `shell_exec`, `http_request` | `Unknown tool: …`, before the permission gate is asked |
| 7 | planner and nova-sparrow in one process | shipped | planner calls `store_note` | **ran**: the note landed in nova-sparrow's memory (0 notes → 1) |

Row 4 emulates ISSUE-0150's grant with the one key today's gate reads
(`network.http`); the effect on `http_request` is the same. Row 5's config
passes `agents/validate.py`, the check `make validate` runs, so nothing warns an
operator that the permission reaches a tool the list leaves out. Row 7 is not
how the agent server hosts agents: it shows that a persona's memory tools, which
register in the same global registry, are just as reachable by name.

## Impact

- **For task agents, the `tools` list is advice, not a limit.** Deny by
  default rests on two lists per agent: `tools`, what it may use, and
  `permissions`, what those tools may touch. At run time only the second holds
  for a task agent. An operator who takes a tool off a task agent's list to
  remove the capability has only removed the offer (row 5).
- **ISSUE-0150 turns the gap into an exposure in the shipped config** (row 4).
  planner, code-writer and code-reviewer each have a `network` block that
  allows two LLM vendors' APIs, and none lists `http_request`. Once the gate
  grants `network:http` from that block, only the model staying inside its
  offer keeps these agents off the network. The exposure is small — two hosts,
  no credentials sent — but new. ISSUE-0150's section 5 names this check as the
  thing to land first; its other option is dropping the three example blocks.
- **The two agent kinds disagree.** The rule an operator can see working on a
  persona does not hold for a task agent.
- **Medium, not high.** In the shipped config the permission gate refuses every
  unlisted call today (rows 1–3). Not low: one of the two deny-by-default
  controls is not enforced, and row 5 is a valid config where that matters now.

## Proposed fix

**1. A task agent runs only the tools it was offered.** `_execute_tools`
answers a call to a tool that is not on the agent's `tools` list with
`Unknown tool: <name>` as an error tool result — the answer a persona gives, and
the one a tool that does not exist already gets, so the model learns nothing
about tools it was not given. The tool does not run. The loop goes on, so the
model can recover and the task can still complete. Listed tools run as before,
still behind the permission gate.

**2. One rule, in `agents/tools/`.** A small new module,
`agents/tools/tool_list.py`, turns an agent's config into the tools it is
offered (for `_build_tool_definitions`) and answers whether one named tool is
offered (for `_execute_tools`). Both methods ask it, so the offer and the run
cannot drift apart again. A `tools:` key with no value, or an entry that is not
a string, names nothing rather than raising an error.

`agents/base.py` is at its 500-line cap and on
[ISSUE-0143](ISSUE-0143-debt-sweep-26-files-at-size-cap.md)'s sweep list (PRs
D3 onward, cuttable). The fix neither waits for that split nor grows the file:
the offer's rationale comments move into the new module with the rule, and
`base.py` comes out shorter.
[ISSUE-0147](ISSUE-0147-unregistered-agent-tools-dropped-silently.md) point 2
proposes the same kind of function for its startup warning; whichever fix lands
second extends this module rather than writing another.

**3. Nothing legitimate reaches a task agent from outside its list.** Checked
before changing the rule:

- A task agent with `memory.enabled: true` (RFC 0008) gets its memories in the
  system prompt (`_inject_memories`), not through tools. The note tools are
  built only for personas (`create_memory_tools`, called from
  `agents/persona.py`).
- The recall tool, `recall_channel_messages`, is added only to agents that have
  `add_recall_tool`; `wire_recall_tools` skips task agents.
- A sub-agent is a task agent, so the same holds for it.

So every tool a task agent can run under the new rule is one it could already
be offered.

**4. Tests first.** A failing pytest in `tests/unit/python/`, with the model
mocked at the `LLMClient` boundary, comes before any code:

- a task agent refuses a registered tool that is not on its list, and the tool
  does not run; a listed tool runs as before;
- end to end through `TaskAgent.handle`: the model names an unlisted tool, the
  error goes back to the model, and the task completes;
- no `tools` key, an empty list, a key with no value, and an entry that is not
  a string all refuse, without an error;
- with an `mcp:` entry and a typo on the list, the tools `_execute_tools` runs
  are exactly the tools `_build_tool_definitions` offers;
- a memory-enabled task agent still gets its memories and still runs its
  listed tool, and a persona's note tool registered in the same process is
  refused (row 7).

Existing tests that have a task agent run a tool it does not list get the tool
added to the list: `test_base_handle.py`, `test_external_tool_wrapping.py` and
the `noop` loop test in `test_agents.py`. Some would fail without it; others
would still pass but stop running the tool they are about.
`test_base_handle.py` is at 486 lines, past the 485-line split point, so its
two tool-list classes (`TestExecuteTools`, `TestBuildToolDefinitions`) move to
the new test file rather than the file growing.

**5. What changes for the shipped agents.** Nothing they can do today: rows 1–3
are refused either way, now with `Unknown tool` before the permission gate is
asked. Once ISSUE-0150 lands, its grant reaches none of the three task agents,
because none lists `http_request`, so ISSUE-0150 can keep the example `network`
blocks.

**Out of scope:**

- the per-task `allowed_tools` filter (`TaskConfig.allowed_tools`), which
  [RFC 0004](../rfcs/0004-python-agent-grpc-server.md#non-goals) defers: the
  orchestrator and the sub-agent spawner send it, and nothing reads it;
- hosting several agents in one process (row 7): the built-in tools read one
  process-wide permission gate (`builtin.permission_gate`, a standing TODO in
  `agents/tools/builtin.py`), and persona tools share the global registry. This
  fix stops a task agent reaching them by name; the rest is separate work;
- logging a refused call: neither agent kind logs one today, and a signal is
  worth adding to both together, if at all;
- the persona copy of the rule, and ISSUE-0147's warning.

## Slot: proposed v0.4.0

- **Not v0.3.16.** That release is in Phase 1, its scope ratified by the
  [sequencing amendment of 2026-08-19](../v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train),
  and this issue is not in it. It is not a
  [fold-in](../methodology/process-glossary.md#fold-in): nothing on v0.3.16's
  critical path needs it, and the shipped config is not exposed while it waits
  (rows 1–3).
- **v0.4.0**, next to ISSUE-0150 and ahead of it: ISSUE-0150's fix should not
  merge before this one unless it drops the three example `network` blocks.
- **[Version-train gate](../methodology/process-glossary.md#version-train-gate):**
  the implementation PR can be written and reviewed now, but it does not merge
  until v0.3.16 is tagged, unless the owner ratifies a dated amendment. The
  v0.4.0 plan opening confirms or moves the slot with a dated note here.

## Notes

> 2026-09-11 — filed from the gap
> [ISSUE-0147](ISSUE-0147-unregistered-agent-tools-dropped-silently.md) notes in
> its Context, which ISSUE-0150's section 5 names as the check to land first.
> The probe in Context was run for this issue. ISSUE-0150 is drafted but not yet
> on `main`; link it here once it lands. Proposed slot v0.4.0; not v0.3.16
> scope.
