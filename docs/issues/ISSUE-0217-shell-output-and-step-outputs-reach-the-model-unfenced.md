---
id: ISSUE-0217
summary: "Two paths handed untrusted text to a model without the `<external_data>` envelope. `shell_exec` output was one: the same file was wrapped when read with `file_read` and bare when a command printed it. Since #1103 a command's output is wrapped and scanned like the other tools'. Still open: a workflow step's output is pasted straight into the next step's input, where the next agent reads it as instructions"
status: in_progress
severity: medium
area: security
created: 2026-11-10
refs:
  - agents/security.py
  - agents/task_tool_round.py
  - agents/persona_runtime/tool_round.py
  - internal/planner/planner.go
  - internal/scheduler/stage_runner.go
  - docs/rfcs/0009-security-sandboxing.md
  - docs/issues/ISSUE-0154-go-and-python-sanitizers-flag-different-text.md
---

# ISSUE-0217: Shell output and step outputs reach the model without the external-data envelope

## Summary

Text that comes from outside is meant to reach a model wrapped in an
`<external_data>` envelope, so the model can tell data from instructions,
and to be scanned for injection patterns. Two paths skip it.

## Context

**Shell output.** `EXTERNAL_TOOL_SOURCES` in `agents/security.py` lists
`http_request`, `file_read` and `recall_channel_messages`. `shell_exec`
is not in the map. The shipped `code-writer` may run `python`, and
`pytest` and `git diff` where the image carries them. Their output is
largely other people's text: file contents, test names, error messages. That output reaches the model as plain tool
output, on success and on a non-zero exit.

**Step outputs.** `ResolveInputs` (`internal/planner/planner.go`) replaces
`{{ steps.<key>.output }}` with the earlier step's raw output. The result
is the next step's `input`, which the receiving agent treats as its task.
Whatever one agent was fed, by a web page or a file, arrives at the next
agent in the position of an instruction.

[ISSUE-0154](ISSUE-0154-go-and-python-sanitizers-flag-different-text.md)
lists which surfaces the injection check covers; these two gaps are not
in it.

## Impact

A prompt-injection attempt planted in a repository file or in a page one
agent fetched has a path to the model, and on to the next agent, that
bypasses the one structural defence the project has. Combined with
[ISSUE-0196](ISSUE-0196-shipped-shell-allowlist-voids-the-file-and-network-rules.md)
that path ends at a shell.

## Proposed fix / investigation path

Add `shell_exec` to `EXTERNAL_TOOL_SOURCES`. Wrap a substituted step
output as agent output before the next agent's model sees it, and say in
the workflow docs that a step's output is data. Which side wraps is a
decision to make first: the Go sanitizer only scans and writes no
envelope, and the agent receives the step input as one string, so it
cannot tell the substituted part from the workflow author's own words.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading the code.

> 2026-11-12 — the shipped `code-writer` no longer lists `python` or any other
> command (ISSUE-0196's config half).

> 2026-11-12 — **the shell half is fixed by
> [#1103](https://github.com/mkhomutov/Persatrix/pull/1103); the step-output
> half stays open.**
> `shell_exec` is now in `EXTERNAL_TOOL_SOURCES`, so what a command prints
> is scanned for injection patterns and reaches the model inside the
> `<external_data>` envelope, on success and on a non-zero exit, where the
> framework's own error line stays outside it. Tests run the real tool
> through both tool rounds. Before the fix, the same file came back fenced
> from `file_read` and bare from a command that printed it.
> The envelope is always there. The `flagged` mark is not: the scan reads
> the output as JSON text, so it flags a phrase that opens stdout or
> stderr or follows a space on its line, and misses one that follows a
> line break or a tab, which `file_read` flags. That gap is shared with
> `http_request` and `recall_channel_messages` and is
> [ISSUE-0235](ISSUE-0235-injection-scan-reads-tool-output-as-json-text.md).
> **What remains:**
>
> - A step's output is still pasted into the next step's input as it is.
>   That is in the workflow engine (`internal/planner`,
>   `internal/scheduler`), which ruling (x) of the
>   [v0.4.x sequencing record](../v0.4.x-sequencing-2026-10-21.md) freezes,
>   and which side wraps is still to decide.
> - No test lists the tools whose output comes from outside and checks
>   each is in `EXTERNAL_TOOL_SOURCES`, so the next such tool can be left
>   off the way `shell_exec` was.
> - A task agent's prompt says nothing about the envelope or its `flagged`
>   mark. The text that explains them
>   (`prompts/runtime/safety/external-data-handling.md`) is loaded only for
>   personas, in `agents/persona_runtime/prompt_assembly.py`, and the one
>   shipped agent that was offered `shell_exec` is a task agent.
>
> Severity is unchanged.
