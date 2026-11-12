---
id: ISSUE-0196
summary: "The shell gate checks only a command's leading words, so a listed interpreter or test runner lets text that steers the model read files the file rules deny, open network connections and print the provider API keys held in the tool's environment. The shipped `code-writer` listed `python`, `pytest`, `ruff` and `git diff`; since #1102 it lists no command and is not offered `shell_exec`, and no tagged release shipped the working list. Open until RFC 0009 Phase 4: a command an operator lists still gets the agent's whole environment, provider keys included"
status: in_progress
severity: high
area: security
created: 2026-11-10
refs:
  - config/agents.yaml
  - agents/tools/permissions.py
  - agents/tools/sandbox.py
  - agents/tools/builtin.py
  - schemas/agent.schema.json
  - prompts/runtime/task-agents/code-writer.md
  - Dockerfile.agent
  - docs/ai-agents-orchestration-spec.md
  - docs/rfcs/0004-python-agent-grpc-server.md
  - docs/rfcs/0009-security-sandboxing.md
  - docs/rfcs/0009-phases-3-4-pr-plan.md
  - docs/issues/ISSUE-0150-schema-valid-config-cannot-enable-shell-exec-or-http-request.md
---

# ISSUE-0196: The shipped shell allowlist cancels the agent's file and network rules

## Summary

`config/agents.yaml` gives the example `code-writer` agent three limits:
paths it may read and write, paths it may never touch (`/workspace/.env`,
anything under `.git`), and commands it may run. It is offered no network
tool, so its network allowlist is never consulted. The command list is
`python`, `pytest`, `ruff` and `git diff`.

A listed command is matched on its leading words only, and its arguments
are never looked at. An interpreter on the list can do anything the
agent's process can do, the network included. So the other limits hold
only while nobody steers the model.

This is live on `main` and in no tagged release. Up to v0.3.16 the gate
refused every `shell_exec` call, because it looked for a config key the
schema does not allow. The fix for
[ISSUE-0150](ISSUE-0150-schema-valid-config-cannot-enable-shell-exec-or-http-request.md)
([#963](https://github.com/mkhomutov/Persatrix/pull/963)) made the list
work. The list itself was already in the shipped config.

## Context

- **The gate.** `PermissionGate.is_command_allowed`
  (`agents/tools/permissions.py`) compares the start of the argument list
  with each allowed entry. `["python", "-c", "<any code>"]` matches
  `python`.
- **Known since RFC 0004.**
  [RFC 0004](../rfcs/0004-python-agent-grpc-server.md#shell-command-argument-injection-known-risk)
  records unrestricted arguments as a known risk, with `python -c` as its
  example. The mitigations it names are the container's limited mounts
  and the path deny list. The deny list is applied by the file tools;
  nothing applies it to what a shell command opens.
- **The environment.** `tool_process_env` (`agents/tools/sandbox.py`)
  removes only variables named `SECURITY_*`. Its docstring says provider
  API keys stay, "since an allowlisted command may need one". On Linux a
  command running as the agent's user can also read the agent's starting
  environment in `/proc/<agent pid>/environ`, which holds the provider
  keys and, when one is set, the fleet enrollment secret.
  [RFC 0009's PR plan](../rfcs/0009-phases-3-4-pr-plan.md) records both
  environment gaps and leaves filing them to its PR 16.
- **What a probe against the shipped permissions showed.** The gate
  admits a `python -c` command that reads `/workspace/.env`, which
  `file_read` refuses; one that prints the environment; one that opens a
  socket to any host; and `git diff --no-index` on a file outside the
  workspace. `pytest` gives the same reach through a `conftest.py`, which
  the agent's write rules allow it to create.
- **The image.** Of the four commands, the shipped agent image carries
  only `python`. `pytest` and `ruff` are development extras and `git` is
  not installed, so in the container those three answer "Command not
  found". `python` alone is enough for everything above.
- **Who can steer the model.** Anyone whose text reaches its prompt: a
  file in the workspace, a workflow input, or an earlier step's output
  ([ISSUE-0217](ISSUE-0217-shell-output-and-step-outputs-reach-the-model-unfenced.md)).
- **The docs.** The orchestration spec's security section repeats the same
  list as its example of a sandboxed agent, and the agent's own
  instructions (`prompts/runtime/task-agents/code-writer.md`) tell it to
  run tests with `shell_exec`.

## Impact

- The file deny list does not hold for this agent, and it has the whole
  network although it is given no network tool.
- The provider API keys, and on Linux the enrollment secret when one is
  set, can be read by the command and sent anywhere.
- From the agent container the command can also reach the orchestrator's
  REST API, which is unauthenticated by default, and the other agents'
  gRPC ports on the compose network.
- Operators copy the shipped example.

## Proposed fix / investigation path

- Take interpreters and test runners out of the shipped example, or move
  them to a second example that says plainly it trusts the workspace.
  Change the agent's instruction to run tests to match.
- Say in the `allowed_commands` description in `schemas/agent.schema.json`
  that a listed command is trusted with every argument it accepts.
- Give a tool's process only the environment variables it names, not
  everything minus `SECURITY_*`. That does not help on its own while an
  interpreter is listed: on Linux the keys stay readable in
  `/proc/<agent pid>/environ`.
- Correct the spec's example and the mitigation RFC 0004 states.
- Settle this before the next release is tagged: no release has shipped
  a working shell list yet. RFC 0009's PR 16 can name this issue for the
  provider-key gap it is due to file.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`. Reproduced by calling the permission gate with the shipped
> config; no command was run against a live agent. The older gate was
> read at the `v0.3.16` tag.

> 2026-11-12 — **the config half is fixed by [#1102](https://github.com/mkhomutov/Persatrix/pull/1102); the environment half
> stays open.**
> The four commands were each run through `shell_exec` in a scratch
> workspace under the shipped rules, and none passed: `python -c` and a
> `conftest.py` under `pytest` ran code that printed the denied `.env`;
> `ruff format --diff .env` and `git diff --no-index /dev/null .env`
> printed it; `ruff check --output-file` and `git diff --output=` wrote a
> file the write rules refuse. So the shipped `code-writer` now lists no
> command and is not offered `shell_exec`. The old list is a commented
> second example for an agent that trusts its workspace, with what each
> command can reach. The agent's instructions, the schema's
> `allowed_commands` description, the spec's example and RFC 0004 (a
> divergence note) say the same, and a test refuses a command on a shipped
> agent that nobody has reviewed. The probe above did not cover `ruff`,
> or that `ruff` and `git diff` write as well as read. What this file says
> of the image was read from `Dockerfile.agent` and `agents/pyproject.toml`;
> no image was built.
> **What remains:** a listed command still gets the agent's environment,
> provider API keys included, and on Linux can read
> `/proc/<agent pid>/environ`. That is RFC 0009 Phase 4's, whose PR 16
> files those gaps. Severity stays high for any config that lists an
> interpreter.
