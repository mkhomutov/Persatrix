---
id: ISSUE-0209
summary: "The orchestrator and the channel schema accept a participant id with capitals, underscores or a single character, such as `User_1`. The agent-side validator requires the stricter agent-id shape, so a message from such a member is stored and fanned out, and every persona then refuses it."
status: open
severity: medium
area: channels
created: 2026-11-10
refs:
  - internal/channels/identifiers.go
  - schemas/channel.schema.json
  - agents/channel_validation.py
  - docs/issues/ISSUE-0094-everyone-broadcast-rejected-by-agent-inbound-validation.md
---

# ISSUE-0209: "Participant id" has two definitions, and personas refuse members the orchestrator accepts

## Summary

Two validators disagree on what a participant id may look like:

| Side | Pattern | Where |
|---|---|---|
| Orchestrator and schema | `^[A-Za-z0-9][A-Za-z0-9_-]*$` | `internal/channels/identifiers.go`, `schemas/channel.schema.json` (`definitions.member`) |
| Agents | `^[a-z0-9][a-z0-9-]*[a-z0-9]$` | `agents/channel_validation.py` |

The Go comment says the pattern is meant to accept human and CLI
participants such as `User_1`. The Python comment says its pattern is the
canonical one from the shared instructions, which is the rule for agent
ids. The Python check is applied to `sender_id` and to every mention.

## Context

The Python validator was given events from the senders `User_1`,
`alice_b`, `a`, `Bob` and `ops-`. All five pass the Go pattern. All five
were rejected with "sender_id is not a valid participant id".

The first-party CLI validates ids against the strict shape before
sending, so the gap is reached by a direct REST caller or by a member
written by hand in `config/channels.yaml`.
[ISSUE-0094](ISSUE-0094-everyone-broadcast-rejected-by-agent-inbound-validation.md)
was the same kind of fault: the orchestrator accepted a value that the
agent-side validator then refused.

No cross-language drift test covers this pair of patterns.

## Impact

A message from such a member is saved to the channel and shown in its
history, and each persona returns a failed acknowledgement, so the member
never gets a reply and nothing tells them why.

## Proposed fix / investigation path

Choose one rule for participant ids and use it on both sides. Then add
the pair to the `test_cross_language_*_drift.py` tests so they cannot
part again.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; reproduced by calling the Python validator directly.
