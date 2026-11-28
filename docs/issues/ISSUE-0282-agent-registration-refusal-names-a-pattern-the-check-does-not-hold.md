---
id: ISSUE-0282
summary: "When the orchestrator refuses an agent's registration for its id, the message says the id must match `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`, a pattern that admits one character. The check itself needs two. An agent registering as `a` is told to match a pattern that `a` matches."
status: open
severity: low
area: server
created: 2026-11-16
refs:
  - internal/server/agent_handlers.go
  - internal/planner/planner.go
  - docs/issues/ISSUE-0209-participant-id-rule-differs-between-go-and-python.md
  - docs/issues/ISSUE-0276-cli-accepts-a-one-character-id.md
  - https://github.com/mkhomutov/Persatrix/pull/1136
---

# ISSUE-0282: The agent registration refusal names a pattern the check does not hold

## Summary

`POST /api/v1/agents` checks the id against the pattern the planner
package exports, `^[a-z0-9][a-z0-9-]*[a-z0-9]$`: at least two
characters. When the id fails, the handler answers `400` with the text
`id must match ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`. That is another
pattern, with the middle and the last character optional, so it admits
one character. The comment above the check quotes the same other
pattern.

## Context

Read in `internal/server/agent_handlers.go`: the check uses the shared
pattern, and the message and the comment are text written beside it.
The rule that is enforced is the right one: it is the participant id
rule, which an agent id shares.

## Impact

A wrong sentence in an error. An agent whose id is one character long is
refused, correctly, with a message it cannot act on: its id matches the
pattern it is shown.

## Proposed fix / investigation path

Build the message from the pattern that is checked, and say the rule in
words as the channels package does (lowercase letters, digits and
hyphens, at least two characters, starting and ending with a letter or
digit). Correct the comment. A test that registers `a` and reads the
message would hold it.

## Notes

> 2026-11-16 — found by the review of pull request 1136, which does not
> change agent registration. Older than that pull request. Severity
> proposed from the tracker's precedent
> ([ISSUE-0276](ISSUE-0276-cli-accepts-a-one-character-id.md), low: the
> same one-character edge, on the CLI's side); not ruled by the
> maintainer.

> 2026-11-28 — severity ruled `low`, as proposed: the maintainer's
> ruling at the
> [strategy review of 2026-11-28](../project-strategy-review-2026-11-28.md#3-the-maintainers-rulings),
> ratified by ruling (ab) of the
> [sequencing Amendment 2026-11-28](../v0.4.x-sequencing-2026-11-28.md#rulings).
