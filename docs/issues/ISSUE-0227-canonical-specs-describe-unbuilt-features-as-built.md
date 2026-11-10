---
id: ISSUE-0227
summary: "The two documents the repository calls its canonical specs are April design documents that still describe unbuilt features in the present tense, security controls among them. Most of the REST routes they list do not exist, and they say all log output is redacted, which is true only of the audit and decision logs."
status: open
severity: medium
area: docs
created: 2026-11-10
refs:
  - docs/ai-agents-orchestration-spec.md
  - docs/persatrix-extension-spec.md
  - docs/documentation-guide.md
  - docs/persatrix-spec-audit.md
  - .github/copilot-instructions.md
  - docs/observability.md
  - internal/server/server.go
  - internal/server/channel_routes.go
  - schemas/agent.schema.json
---

# ISSUE-0227: The two "canonical" specs describe unbuilt features as built

## Summary

`docs/documentation-guide.md` calls
`docs/ai-agents-orchestration-spec.md` and
`docs/persatrix-extension-spec.md` "the canonical design specs — update
when behavior changes". The shared assistant instructions point to them
as the full details of the project. Neither carries a note that it is a
design from before most of the code existed.

## Context

Claims checked against the code:

| The spec says | What is true |
|---|---|
| §8.3 lists 42 REST routes | 25 are not registered (organizations, agent state, sub-agents, observers, evaluations, nodes, mesh, A2A, bridges, streams). About 25 real routes are missing from the list (auth, chat, members, config, convene, unquarantine, log stream). |
| "Redaction in logs — all log output is scanned for known secret patterns and redacted" | The default log redactor is a pass-through; only the audit log and the decision log redact. `docs/observability.md` says so. |
| An egress proxy, a chroot, `<tool_result>` tagging and audit input and output hashes protect tool use | No code refers to any of them. |
| `store_get` and `store_set` are tools that are always available | They do not exist. |
| `from Persatrix.tools import tool` | The decorator lives in `agents/tools/registry.py`. |
| The project tree has `agents/coder.py`, `reviewer.py`, `planner.py` and `internal/telemetry/` | None exist. |
| The extension spec's persona example | It fails the shipped agent schema: `behavior` is required and `personality` is not allowed. |

`docs/persatrix-spec-audit.md` reports all 45 of its issues resolved,
but it compares the two specs with each other, not with the code.

## Impact

A reader, human or assistant, who follows the pointer to "full details"
is told that controls exist which do not, and is given an API that is
mostly not there. The security claims are the costly part.

## Proposed fix / investigation path

Put a banner at the top of both files: a historical design, not a
description of what is built, with links to the guides and the ROADMAP
for current behaviour. Change the documentation guide's wording to
match. Generate the route list from the server's route table, or delete
it.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; the rows above were checked by reading, and the persona
> example was run through the schema.
