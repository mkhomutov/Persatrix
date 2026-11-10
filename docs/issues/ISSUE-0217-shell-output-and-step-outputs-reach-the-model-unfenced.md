---
id: ISSUE-0217
summary: "Two paths hand untrusted text to a model without the `<external_data>` envelope. `shell_exec` output is not listed as an external source, so the same file is wrapped when read with `file_read` and not wrapped when a shell command prints it. A workflow step's output is pasted straight into the next step's input, where the next agent reads it as instructions."
status: open
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
