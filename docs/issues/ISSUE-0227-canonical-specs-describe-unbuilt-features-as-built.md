---
id: ISSUE-0227
summary: "The two documents the repository calls its canonical specs are April design documents that still describe unbuilt features in the present tense, security controls among them. Most of the REST routes they list do not exist, and they say all log output is redacted, which is true only of the audit and decision logs."
status: resolved
severity: medium
area: docs
created: 2026-11-10
closed: 2026-11-14
refs:
  - CLAUDE.md
  - docs/issues/ISSUE-0202-workflow-condition-and-approval-validate-then-do-nothing.md
  - docs/issues/ISSUE-0214-declared-tool-permissions-are-never-enforced.md
  - docs/ai-agents-orchestration-spec.md
  - docs/persatrix-extension-spec.md
  - docs/documentation-guide.md
  - docs/persatrix-spec-audit.md
  - .github/copilot-instructions.md
  - docs/observability.md
  - internal/server/server.go
  - internal/server/channel_routes.go
  - agents/security.py
  - schemas/agent.schema.json
---

# ISSUE-0227: The two "canonical" specs describe unbuilt features as built

## Summary

`docs/documentation-guide.md` calls
`docs/ai-agents-orchestration-spec.md` and
`docs/persatrix-extension-spec.md` "the canonical design specs — update
when behavior changes". The shared assistant instructions point to them
as the full details of the project. Neither has a banner at its top
saying it is a design from before most of the code existed. The
orchestration spec has three notes inside single sections, one of them
"Planned — not built yet"; the extension spec has none.

## Context

Claims checked against the code:

| The spec says | What is true |
|---|---|
| §8.3 lists 42 REST routes | 25 are not registered (organizations, agent state, sub-agents, observers, evaluations, nodes, mesh, A2A, bridges, streams). About 25 real routes are missing from the list (auth, chat, members, config, convene, unquarantine, log stream). |
| "Redaction in logs — all log output is scanned for known secret patterns and redacted" | The default log redactor is a pass-through; only the audit log and the decision log redact. `docs/observability.md` says so. |
| An egress proxy, a chroot, `<tool_result>` tagging and audit input and output hashes protect tool use | No code refers to the proxy, the chroot or the hashes. The tagging exists under another name: `agents/security.py` wraps three tools' output in `<external_data>`. |
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
it. Decide whether the notes inside sections continue beside the banner.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; the rows above were checked by reading, and the persona
> example was run through the schema.

> 2026-11-12 — two more rows of the same kind, in the orchestration spec's
> permission example (§6.2): its `shell.deny_commands` list and its whole
> `mcp` block (`allowed_servers`, `denied_servers`) are keys
> `schemas/agent.schema.json` refuses, so a config copied from the example
> fails `make validate`. Seen while
> [#1102](https://github.com/mkhomutov/Persatrix/pull/1102) corrected the
> same example's command list; left as they are for this issue.

> 2026-11-14 — resolved. What changed:
>
> - **A banner at the top of both specs**: an April 2026 design written
>   before most of the code, not a description of what is built, with links
>   to the guides, the observability doc and the ROADMAP.
> - **A note at each claim** in the table above and in the 2026-11-12 note,
>   in the two forms the orchestration spec already used: "Planned, not
>   built yet" for a whole section, and a dated "what is built" note where
>   part exists. Each says what is true today and links the guide or source
>   file that shows it. A reader who lands in the middle of a file meets
>   the note without the banner.
> - **The route list in §8.3 is removed**, with a pointer to the files that
>   register the routes, the file that says who may call each, and the
>   guides. A list written by hand would go out of date the same way, and a
>   generated one needs a generator and a check, which is more than a
>   documentation change.
> - **The texts that sent readers to the specs** are reworded: the
>   documentation guide (three places), the "Full details" line of the
>   shared assistant instructions, the "Specs" line of `CLAUDE.md`. The
>   spec audit says it compared the two specs with each other, not with the
>   code. A comment in `internal/server/server.go` that asked for the
>   removed list to be extended is gone.
>
> Every row above was checked again against `main` at `fb0e69b8`. Three
> were out of date or incomplete:
>
> - **Routes**: of the 42 listed, 17 are served and 25 are not, as the row
>   says. The orchestrator serves 23 that the list lacked, not "about 25".
> - **`<external_data>`**: four tools' output is wrapped now, not three;
>   `shell_exec` joined them on 2026-11-12
>   ([ISSUE-0217](ISSUE-0217-shell-output-and-step-outputs-reach-the-model-unfenced.md)).
> - **The persona example** fails the schema on more than the row says:
>   the agent also needs `type` and `role`, and `autonomy.can_refuse_tasks`
>   is refused.
>
> The sweep of both specs for other security claims found these, each now
> with a note:
>
> - A refused permission is not "logged as a security event": it is a
>   warning in the agent's log.
> - Not "every tool invocation passes through a permission gate": a custom
>   tool's declared permissions are never checked
>   ([ISSUE-0214](ISSUE-0214-declared-tool-permissions-are-never-enforced.md)),
>   and the "iron rule" that a sub-agent gets no more than its parent is
>   not enforced.
> - Secrets are not `${ENV_VAR}` references in YAML, and "never in LLM
>   context" is not enforced: a shell command's environment keeps the
>   provider API keys.
> - Human approval gates are not built
>   ([ISSUE-0202](ISSUE-0202-workflow-condition-and-approval-validate-then-do-nothing.md)),
>   nor output validation or field filtering.
> - The audit log records security events and one line for each task sent
>   to an agent, not each tool call; no event raises an alert; nothing
>   outside the tests checks the checksum chain.
> - Rate limiting is one per-agent call limit and a login throttle, not the
>   block of limits the spec lists.
> - Bridges, the mesh, A2A and observer mode are not built, so none of
>   their safeguards exists: no bridge approval or audit, no mutual TLS, no
>   node admission, no trust domains, no encryption of messages, no
>   observer consent levels. The orchestrator and its agents talk over gRPC
>   without TLS.
>
> Authentication, which the orchestration spec lists as out of scope, has
> been built since; its line now points to the authentication guide.
>
> Both specs stay on the size check's allow-list and grew: the
> orchestration spec from 3,612 to 5,072 words by the checker's count, the
> extension spec from 4,260 to 4,907. The removed route list sat in code
> blocks, which the checker does not count.
