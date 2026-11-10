---
id: ISSUE-0196
summary: "The shipped `code-writer` agent may run `python` and `pytest` through `shell_exec`. The shell gate checks only a command's leading words, so text that steers the model can make it read files the file rules deny, open connections the network rules do not list, and print the provider API keys held in the tool's environment."
status: open
severity: high
area: security
created: 2026-11-10
refs:
  - config/agents.yaml
  - agents/tools/permissions.py
  - agents/tools/sandbox.py
  - agents/tools/builtin.py
  - schemas/agent.schema.json
  - docs/ai-agents-orchestration-spec.md
  - docs/rfcs/0009-security-sandboxing.md
  - docs/issues/ISSUE-0150-schema-valid-config-cannot-enable-shell-exec-or-http-request.md
---

# ISSUE-0196: The shipped shell allowlist cancels the agent's file and network rules

## Summary

`config/agents.yaml` gives the example `code-writer` agent four limits:
paths it may read and write, paths it may never touch (`/workspace/.env`,
anything under `.git`), commands it may run, and hosts it may call. The
command list is `python`, `pytest`, `ruff` and `git diff`.

A listed command is matched on its leading words only, and its arguments
are never looked at. An interpreter on the list can do anything the
agent's process can do. So the other three limits hold only while nobody
steers the model.

## Context

- **The gate.** `PermissionGate.is_command_allowed`
  (`agents/tools/permissions.py`) compares the start of the argument list
  with each allowed entry. `["python", "-c", "<any code>"]` matches
  `python`.
- **The environment.** `tool_process_env` (`agents/tools/sandbox.py`)
  removes only variables named `SECURITY_*`. Its docstring says provider
  API keys stay, "since an allowlisted command may need one".
- **What a probe against the shipped permissions showed.** The gate
  admits a `python -c` command that reads `/workspace/.env`, which
  `file_read` refuses; one that prints the environment; one that opens a
  socket to any host; and `git diff --no-index` on a file outside the
  workspace. `pytest` gives the same reach through a `conftest.py`, which
  the agent's write rules allow it to create.
- **Who can steer the model.** Anyone whose text reaches its prompt: a
  file in the workspace, a workflow input, or an earlier step's output
  ([ISSUE-0217](ISSUE-0217-shell-output-and-step-outputs-reach-the-model-unfenced.md)).
- **The docs.** The orchestration spec's security section repeats the same
  list as its example of a sandboxed agent.

## Impact

- The file deny list and the network allowlist do not hold for this agent.
- The provider API keys can be read by the command and sent anywhere.
- From the agent container the command can also reach the orchestrator's
  REST API, which is unauthenticated by default, and the other agents'
  gRPC ports on the compose network.
- Operators copy the shipped example.

## Proposed fix / investigation path

- Take interpreters and test runners out of the shipped example, or move
  them to a second example that says plainly it trusts the workspace.
- Say in the `allowed_commands` description in `schemas/agent.schema.json`
  that a listed command is trusted with every argument it accepts.
- Give a tool's process only the environment variables it names, not
  everything minus `SECURITY_*`.
- Correct the spec's example.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`. Reproduced by calling the permission gate with the shipped
> config; no command was run against a live agent.
