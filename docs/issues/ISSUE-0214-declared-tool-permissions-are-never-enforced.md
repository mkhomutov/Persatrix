---
id: ISSUE-0214
summary: "Three permission mechanisms that the docs present as working are stored and never checked: the `permissions` a custom tool declares with `@tool`, the `allowed_tools` list on a task or delegation, and the rule that a sub-agent gets no more than its parent. The framework's own nine tools each check the gate in their own body; nothing checks it on a tool's behalf. The assistant instructions, three glossary entries and the spec's notes now say so, other texts still do not (ISSUE-0272), and nothing is enforced yet."
status: resolved
severity: medium
area: agents/tools
created: 2026-11-10
closed: 2026-11-26
closed_pr: 1148
refs:
  - agents/tools/registry.py
  - agents/tools/permissions.py
  - agents/tools/tool_list.py
  - agents/task_tool_round.py
  - agents/persona_runtime/tool_round.py
  - agents/tools/memory_tools.py
  - agents/tools/recall.py
  - schemas/agent.schema.json
  - agents/task_types.py
  - agents/sub_agents/spawner.py
  - agents/sub_agents/merge.py
  - docs/ai-agents-orchestration-spec.md
  - .github/copilot-instructions.md
  - .github/instructions/python-agents.instructions.md
  - docs/issues/ISSUE-0151-task-agents-run-tools-missing-from-their-list.md
  - docs/ai-glossary.md
  - agents/persona.py
  - agents/action_executor.py
  - agents/sub_agents/delegation.py
  - tests/unit/python/test_declared_tool_permissions.py
  - tests/unit/python/test_tool_permission_names.py
  - tests/unit/python/test_task_allowed_tools.py
  - tests/unit/python/test_builtin_tools_declared_permissions.py
  - tests/unit/python/test_shipped_agents_tool_admission.py
  - docs/issues/ISSUE-0285-sub-agent-permission-rule-and-delegation-timeout-are-not-built.md
  - docs/issues/ISSUE-0147-unregistered-agent-tools-dropped-silently.md
  - docs/issues/ISSUE-0256-nothing-verifies-the-audit-chain-and-tool-denied-is-never-emitted.md
---

# ISSUE-0214: Declared tool permissions are never enforced

## Summary

| Mechanism | What happens to it | Where |
|---|---|---|
| `@tool(permissions=[...])` | Stored on the tool definition. No production code reads it. The wrapper carries `# TODO: Permission check before execution`. | `agents/tools/registry.py` |
| `allowed_tools` on a task or delegation | Carried from the wire and marked `TODO(v0.2): enforce`. The Go side never sets it. | `agents/task_types.py` |
| A sub-agent gets at most its parent's permissions | `# TODO: Implement sub-agent permission inheritance (child <= parent)` | `agents/tools/permissions.py` |

## Context

- The framework's own nine tools each call the permission gate inside
  their own function, so they hold: `file_read`, `file_write`,
  `shell_exec`, `http_request`, the four note tools and
  `recall_channel_messages`.
- A custom tool does not. The spec's own example declares
  `permissions=["db:read"]`; such a tool runs for any agent that lists
  it, whatever that agent's `permissions` block says.
- The shared assistant instructions list "sub-agents inherit restricted
  permissions" as a key pattern of the project.
- Two smaller faults in the same contract: a delegation's
  `budget.timeout_seconds` is serialised and never read, and a result
  whose `tags` are not strings raises out of `merge_result` instead of
  being rejected entry by entry (reproduced).
- `SubAgentSpawner` has no production caller yet, so the sub-agent half
  is latent.
  [ISSUE-0151](ISSUE-0151-task-agents-run-tools-missing-from-their-list.md)
  records the `allowed_tools` deferral.

## Impact

Deny-by-default depends on each tool author remembering to call the
gate. The first custom tool that touches a database or an external
system will run without the check its declaration implies.

## Proposed fix / investigation path

Check a tool's declared permissions in the `@tool` wrapper or in the one
admission function both agent kinds use. First decide how a custom
permission is granted: the agent schema's `permissions` block allows five
categories and no others, so `db:read` cannot be granted today, and a
wrapper check alone would refuse every custom tool. Enforce
`allowed_tools` in the tool round. Correct the spec and the two instruction lines until then,
and enforce or delete the sub-agent fields before anything calls the
spawner.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading the code.

> 2026-11-14 — the spec half of "correct the spec and the two instruction
> lines" is done by the fix for
> [ISSUE-0227](ISSUE-0227-canonical-specs-describe-unbuilt-features-as-built.md):
> the orchestration spec now carries a note at the custom-tool example, at
> the permission gate's "every tool invocation" claim and at its sub-agent
> rule, and the extension spec at its own sub-agent rule (E3.2.4), the
> sub-agent lifecycle (E3.2.7) and the decisions table (E14). Each says
> the check is not enforced and links here. The two instruction lines are
> unchanged and still part of this issue.

> 2026-11-15 — the doc half is done in
> [#1133](https://github.com/mkhomutov/Persatrix/pull/1133); the code half
> remains, so this stays `in_progress`. Each mechanism was checked against `main` at `111b041d`
> first. `@tool` stores `permissions` on the tool's definition and its
> wrapper still carries the `TODO`; no code reads the stored list.
> `allowed_tools` is copied from the wire into the task's config and
> from a delegation request into the spawner, and no tool round reads
> it. The parent rule is still a `TODO` in `agents/tools/permissions.py`.
> And no sub-agent can be spawned at all: `PersonaAgent.spawn_sub_agent`
> forwards to a client that nothing provides, the `spawn_sub_agent`
> action answers `not_implemented`, and the spawner has no caller
> outside tests.
>
> What now says so: the two instruction lines (the shared assistant
> instructions and the Python instructions), each with what to do
> instead, which is to check the permission gate in the tool's own body
> as the built-in tools do; and the glossary's Sub-agent and Tool
> entries. Two glossary examples also named permissions that do not
> exist (`fs.read`, `net.http`); the gate's names are `filesystem:read`
> and, for outbound HTTP, the `network.allow` list.
>
> What remains: the proposed fix above, which starts with a decision on
> how a custom permission is granted. Two texts in the code still
> describe the checks as made and belong to that half: the module
> docstring of `agents/tools/permissions.py` ("Checks every tool
> invocation") and the docstring of `PersonaAgent.spawn_sub_agent`
> ("The framework handles: Permission validation (child ≤ parent)").

> 2026-11-16 — corrections after the review of [#1133](https://github.com/mkhomutov/Persatrix/pull/1133).
>
> "Sub-agents are not built" said too much. An in-process spawner
> exists (`agents/sub_agents/spawner.py`) and tests run it; what is true
> is that nothing a persona or the orchestrator does reaches it. The
> instruction lines and the glossary's Sub-agent entry now say "not in
> use", as the spec's note of 2026-11-14 did.
>
> "Check the permission gate in the tool's own body" cannot be followed
> for a permission of the tool's own. The agent schema has five
> permission categories (`filesystem`, `network`, `shell`, `memory`,
> `channels`) and allows no other key, so a new name could never be
> granted. The lines now say so, and name the gate by its class,
> `PermissionGate`. This is the first thing the code half has to
> decide.
>
> The summary said the spec, the instructions and the glossary now say
> so. That holds for the instruction files, the glossary's Sub-agent,
> Tool and Permission entries and the spec's dated notes. Eight other
> places still present delegation, sub-agent spawns or tool permission
> checks as working; they are listed in
> [ISSUE-0272](ISSUE-0272-other-docs-still-present-delegation-and-permission-checks-as-working.md).

> 2026-11-26 — resolved by
> [#1148](https://github.com/mkhomutov/Persatrix/pull/1148), on the
> maintainer's three choices.
>
> Both faults were first shown on `main`: a tool that declared
> `filesystem:read` ran for a task agent and for a persona with no
> `filesystem` block, and a task whose `allowed_tools` named one tool was
> offered, and ran, every tool on its agent's list.
>
> **1. What can be granted today is enforced.** `admit_call`
> (`agents/tools/tool_list.py`), the one function through which both agent
> kinds run a tool, refuses a model's call unless the agent's own
> `permissions` block grants every permission the tool declares. The model
> is told `Permission denied: <permission>`, the words each built-in tool
> already gave from its own body; the operator gets a warning; the call
> counts as a failed tool call. The check sits there, and not in the
> `@tool` wrapper, because only the tool round knows which agent is
> calling. The nine built-in tools keep their own checks.
>
> A tool can declare seven permissions, the ones an agent's config can
> grant: `filesystem:read`, `filesystem:write`, `network:http`,
> `shell:exec`, `memory:read`, `memory:write` and `channels:recall`. Any
> other name raises `ValueError` when the tool is defined. For a name
> outside the schema's five categories the message says that a tool cannot
> have a permission of its own yet. The agent schema was not opened for
> custom names: that was considered and not taken. A name inside the five
> categories that no key grants, such as `filesystem:deny`, is refused the
> same way, and the gate now answers "denied" for it: before, it read the
> config key of that name, so `network:deny` was granted by a deny list.
>
> A tool that declares no permission is held only by the agent's `tools`
> list, as before. A tool function called directly from Python, not by a
> model, passes no check but its own.
>
> **2. A task's `allowed_tools` is enforced** in both tool rounds
> (`task_allows`): a tool the list does not name is not offered, and a
> call to it gets `Unknown tool`. An empty list is no restriction from this
> field: the wire cannot tell an empty list from an unset one, and the
> orchestrator never sets it, so no shipped run changes. The list narrows
> the agent's own `tools` list and never adds to it. A delegation's list
> reaches its sub-agent the same way, as the task's.
>
> **3. The sub-agent rule is deferred** to
> [ISSUE-0285](ISSUE-0285-sub-agent-permission-rule-and-delegation-timeout-are-not-built.md),
> with the delegation's unread `budget.timeout_seconds`. Nothing reaches
> the spawner. The two docstrings that said the rule was enforced are
> corrected.
>
> The smaller fault is fixed too: a result entry whose `tags` are not text
> is rejected alone (`schema_invalid`), where it raised out of
> `merge_result`; and a `tags` value that is not a list no longer raises
> while the result is read.
>
> No shipped agent loses a tool. The check refuses a shipped agent only
> where the tool's own body already did, with the same words: `file_read`
> for the three personas, which list it without a `filesystem` block, and
> the recall tool for the two personas without `channels.recall`
> ([ISSUE-0147](ISSUE-0147-unregistered-agent-tools-dropped-silently.md)
> records that gap). `tests/unit/python/test_shipped_agents_tool_admission.py`
> holds this, and passes unchanged before and after the change.
>
> Not done here: a refused call still writes no `tool.denied` audit line
> ([ISSUE-0256](ISSUE-0256-nothing-verifies-the-audit-chain-and-tool-denied-is-never-emitted.md)).
