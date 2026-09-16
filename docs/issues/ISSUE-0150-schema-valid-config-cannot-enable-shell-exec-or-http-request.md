---
id: ISSUE-0150
summary: "No agent config that passes `make validate` can turn on `shell_exec` or `http_request`: the permission gate looks for `shell.exec` and `network.http` keys that the agent schema forbids, and the tool tests hand the gate those keys directly — and `shell.max_execution_seconds`, set to 30 on the shipped code-writer, is read by nothing; proposes that each tool's allowlist be its grant and that the limit cap each command"
status: open
severity: medium
area: agents/tools
created: 2026-09-10
refs:
  - agents/tools/permissions.py
  - agents/tools/builtin.py
  - agents/server_persona.py
  - agents/base.py
  - agents/tools/tool_list.py
  - schemas/agent.schema.json
  - config/agents.yaml
  - tests/unit/python/test_permissions.py
  - tests/unit/python/test_builtin_tools_filesystem_shell.py
  - tests/unit/python/test_builtin_tools_http.py
  - tests/unit/python/test_builtin_tools_schema_valid_config.py
  - docs/ai-agents-orchestration-spec.md
  - docs/rfcs/0009-security-sandboxing.md
  - docs/issues/ISSUE-0151-task-agents-run-tools-missing-from-their-list.md
---

## Summary

Two of the four built-in tools cannot be turned on by any agent config that
passes `make validate`. Before it does anything, `shell_exec` asks the
permission gate for `shell:exec`, and `http_request` asks for `network:http`.
The gate says yes only if the agent's `permissions` block has a `shell.exec`
or `network.http` key set, and the agent schema forbids both keys. So the
shipped code-writer, which lists `shell_exec` and allowlists four commands,
can run none of them. The time limit it sets, `max_execution_seconds: 30`,
does nothing either: no code reads it.

## Context

**How a tool is turned on.** Each agent in `config/agents.yaml` has a
`permissions` block saying what its tools may touch, and
`schemas/agent.schema.json` fixes which keys that block may hold: any other key
fails validation. Before a built-in tool (`agents/tools/builtin.py`) acts, it
asks the permission gate (`PermissionGate.check` in
`agents/tools/permissions.py`) for a permission named `category:action`. The
gate looks up `permissions[category][action]` and says yes only if it holds a
non-empty list or `true`.

**Where the two disagree.** A probe on 2026-09-10, at `d0c5675a`, checked each
built-in tool:

| Tool | Asks the gate for | Gate looks for | The schema allows | Result |
|---|---|---|---|---|
| `file_read` | `filesystem:read` | `filesystem.read` | `read`, `write`, `deny` | works |
| `file_write` | `filesystem:write` | `filesystem.write` | `read`, `write`, `deny` | works |
| `shell_exec` | `shell:exec` | `shell.exec` | `allowed_commands`, `max_execution_seconds` | always refused |
| `http_request` | `network:http` | `network.http` | `allow`, `deny` | always refused |

The memory tools and the recall tool ask for `memory:read`, `memory:write` and
`channels:recall`; the schema has those keys, so they work. For the two broken
tools, a test wrote a one-agent config, checked it with the validator
`make validate` runs, loaded it through `load_agent`
(`agents/server_persona.py`) and called the tool. It got
`Permission denied: shell:exec` for an allowlisted command and
`Permission denied: network:http` for an allowlisted domain.

**Why nothing caught it.** Three things hid it:

- The gate and tool tests build the gate from a hand-written dict, never from a
  validated config: `{"shell": {"exec": True}}` in `test_permissions.py`, and
  `exec: True` and `http: True` in the fixtures of
  `test_builtin_tools_filesystem_shell.py` and `test_builtin_tools_http.py`. A
  test named `test_shell_exec_granted` checked `shell:allowed_commands`, a
  permission no tool asks for, so it passed while the real one could not.
- `load_agent` does not validate. It builds the gate from whatever the file
  holds, so a config that skips `make validate` and adds `exec: true` does get
  the tool.
- CI's "Validate configs" job checks configs against the schema, but nothing
  checks that the schema can hold what the tools ask for.

**The unread limit.** `shell.max_execution_seconds` is in the schema, the
shipped config, one test fixture and the permission example in the
[orchestration spec](../ai-agents-orchestration-spec.md#62-permission-system)
(§6.2), and no code in `agents/` or `internal/` reads it. The limit that
applies is `MAX_TIMEOUT_SECONDS = 300` in `builtin.py`, which caps the timeout
the model asks for (30 unless it asks otherwise). An operator who got the tool
running the way the tests do would get a 300-second ceiling under a config that
says 30.

## Impact

- **The shipped example does not do what it says.** code-writer's role is to
  write tested code, and it cannot run a test, though its config lists the tool
  and the commands.
- **`http_request` cannot be enabled at all.** The shipped config's comment says
  an agent that adds `http_request` "gets both domains"; it would get neither.
- **A stated safety limit is not enforced.** Harmless while the tool is dead; it
  matters as soon as anyone gets the tool running, with or without this fix.
- **Medium, not high.** Both tools fail closed: nothing is granted that should
  be refused.

## Proposed fix

**1. The allowlist is the grant.** `shell:exec` is granted by a non-empty
`shell.allowed_commands`, and `network:http` by a non-empty `network.allow`.
The same list still limits which commands run and which domains are called; an
empty or missing list keeps the tool off. This is the rule the filesystem tools
already follow, and it matches §6.2 of the orchestration spec, which scopes the
shell with `allowed_commands` and the network with `allow` and `deny`, with no
switch beside them. The alternative, adding `exec` and `http` booleans to the
schema, was rejected: it keeps the trap that caused this bug — an operator
writes the list, misses the switch, and is refused without a word — and a
switch set to `true` beside an empty list would still refuse everything.

Every config `make validate` accepts either behaves as before or gains the tool
it listed. `exec` and `http` keys stop meaning anything; the schema never
allowed them. One config changes meaning, and `make validate` already rejects
it: `exec: false` beside a non-empty list is refused today and would run the
listed commands.

**2. `max_execution_seconds` caps each command.** The model still picks a
timeout; the agent's limit is the most it gets, and `MAX_TIMEOUT_SECONDS` stays
the ceiling no config can raise. The schema gains `minimum: 1` and
`maximum: 300`, so a value the tool would not honour fails validation instead
of being quietly lowered. Wiring it in beats removing it: the spec shows the
key in this exact place, removing it would break validation for every config
that sets it, and turning the tool on without it would give code-writer a
300-second ceiling under a config that says 30. So the grant and the limit land
in one PR. [RFC 0009](../rfcs/0009-security-sandboxing.md#d-execution-sandboxing--resource-limits)
§D proposes the same 30-second default for shell wall time, and its Phase 3
(v0.4.0) adds a `resource_limits:` block for CPU time and output size. That
phase should adopt this key as the shell wall-time setting, or migrate it,
rather than add a second one; the implementation PR adds that pointer to the
RFC.

**3. A guard for the whole class.** A new test reads, from the source, every
permission a tool passes to `gate.check`. For each, it tries every key the
schema allows in that category, set to a "yes" value of its type, and requires
at least one to grant it. It fails today on `shell:exec` and `network:http`,
and will fail for any future tool that asks for a key the schema cannot hold.

**4. Tests first.** A new file,
`tests/unit/python/test_builtin_tools_schema_valid_config.py`, drives each tool
the long way: write a config, validate it, load it with `load_agent`, call the
tool. It pins that the list grants the tool and still limits it, that an empty
or missing list refuses it, that `max_execution_seconds` cuts a command off,
and that the filesystem tools keep working. The existing fixtures drop their
`exec` and `http` keys, so they no longer describe a config the schema rejects.

**5. What changes for the shipped agents.** code-writer can use `shell_exec`,
capped at 30 seconds. The gate also starts granting `network:http` to planner,
code-writer and code-reviewer, whose `network` blocks are examples: none lists
`http_request`, so none is offered it. But a task agent runs any registered
tool the model names, listed or not (`_execute_tools` in `agents/base.py`;
[ISSUE-0147](ISSUE-0147-unregistered-agent-tools-dropped-silently.md) notes
that personas refuse such calls). Today the gate refuses `http_request` for all
three; afterwards, only the model not being offered the tool stands in the way.
The allowed hosts are two LLM vendors' APIs and the tool sends no credentials,
so the exposure is small, but it is new. Two ways to close it: land this fix
after a check that a task agent runs only the tools on its list
([ISSUE-0151](ISSUE-0151-task-agents-run-tools-missing-from-their-list.md)),
or drop the three example `network` blocks in the same PR. The check needs room in
`agents/base.py`, which is at its 500-line cap and on the v0.3.16 debt sweep's
list ([ISSUE-0143](ISSUE-0143-debt-sweep-26-files-at-size-cap.md), PRs D3…,
cuttable).

## Slot: proposed v0.4.0

- **Not v0.3.16.** That release is in Phase 1, its scope ratified by the
  [sequencing amendment of 2026-08-19](../v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train),
  and this issue is not in it. It is not a
  [fold-in](../methodology/process-glossary.md#fold-in): nothing on v0.3.16's
  critical path needs it, and nothing is exposed while it waits. Taking it into
  v0.3.16 would need a new dated amendment, decided openly; it is small, with no
  store migration and no RFC, so it could ride as a cuttable item if the owner
  wants it.
- **v0.4.0** is the next ratified version, and it carries RFC 0009 Phase 3,
  which owns the resource limits this touches.
- **[Version-train gate](../methodology/process-glossary.md#version-train-gate):**
  the implementation PR can be written and reviewed now, but it does not merge
  until v0.3.16 is tagged. The v0.4.0 plan opening confirms or moves the slot
  with a dated note here.

## Notes

> 2026-09-10 — found during the review of
> [#903](https://github.com/mkhomutov/Persatrix/pull/903) and filed with the
> implementation drafted test-first. Against `d0c5675a`, the new and updated
> tests gave 48 failures; with the gate change alone, 8, all on the time limit;
> with the limit wired in, none. Proposed slot v0.4.0; not v0.3.16 scope.
