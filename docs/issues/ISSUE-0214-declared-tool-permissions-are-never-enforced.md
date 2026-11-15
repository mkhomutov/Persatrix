---
id: ISSUE-0214
summary: "Three permission mechanisms that the docs present as working are stored and never checked: the `permissions` a custom tool declares with `@tool`, the `allowed_tools` list on a task or delegation, and the rule that a sub-agent gets no more than its parent. The framework's own nine tools each check the gate in their own body; nothing checks it on a tool's behalf. The spec, the instructions and the glossary now say so; nothing is enforced yet."
status: in_progress
severity: medium
area: agents/tools
created: 2026-11-10
refs:
  - agents/tools/registry.py
  - agents/tools/permissions.py
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
