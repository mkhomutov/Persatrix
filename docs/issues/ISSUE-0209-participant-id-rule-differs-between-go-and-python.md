---
id: ISSUE-0209
summary: "The orchestrator's channels package and the channel schema accept a participant id with capitals, underscores or a single character, such as `User_1`. The agent-side validator, like the accounts package and the CLI, requires the stricter shape, so a message from such a member is stored and fanned out, and every persona then refuses it."
status: open
severity: medium
area: channels
created: 2026-11-10
refs:
  - internal/channels/identifiers.go
  - schemas/channel.schema.json
  - agents/channel_validation.py
  - agents/participant.py
  - internal/accounts/accounts.go
  - cli/src/validation.rs
  - docs/issues/ISSUE-0094-everyone-broadcast-rejected-by-agent-inbound-validation.md
---

# ISSUE-0209: "Participant id" has two definitions, and personas refuse members the orchestrator accepts

## Summary

Two rules are in use for what a participant id may look like:

| Rule | Pattern | Where |
|---|---|---|
| Loose | `^[A-Za-z0-9][A-Za-z0-9_-]*$` | The channels package (`internal/channels/identifiers.go`) and four places in `schemas/channel.schema.json` |
| Strict | `^[a-z0-9][a-z0-9-]*[a-z0-9]$` | The agents (`agents/participant.py`, `agents/channel_validation.py`), the accounts package (`internal/accounts/accounts.go`) and the CLI (`cli/src/validation.rs`) |

The loose rule is the outlier. Its Go comment says it is meant to accept
human and CLI participants such as `User_1`, yet the CLI and the accounts
package hold the strict one. The Python check is applied to `sender_id`,
to every mention, to `floor_mentions` and to `thread_parent_sender_id`.

## Context

The Python validator was given events from the senders `User_1`,
`alice_b`, `a`, `Bob` and `ops-`. All five pass the Go pattern. All five
were rejected with "sender_id is not a valid participant id". A reply
threaded under a message from `User_1` is refused as well.

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

Choose one rule for participant ids and use it everywhere; the strict one
is already held by every place but the channels package and its schema.
Then add
the pair to the `test_cross_language_*_drift.py` tests so they cannot
part again.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; reproduced by calling the Python validator directly.
