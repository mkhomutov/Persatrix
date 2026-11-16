---
id: ISSUE-0209
summary: "The orchestrator's channels package and the channel schema accept a participant id with capitals, underscores or a single character, such as `User_1`. The agent-side validator, like the accounts package and the CLI, requires the stricter shape, so a message from such a member is stored and fanned out, and every persona then refuses it."
status: resolved
severity: medium
area: channels
created: 2026-11-10
closed: 2026-11-17
closed_pr: 1136
refs:
  - internal/channels/identifiers.go
  - internal/channels/legacy_participant_ids.go
  - schemas/channel.schema.json
  - agents/channel_validation.py
  - agents/participant.py
  - internal/accounts/accounts.go
  - cli/src/validation.rs
  - tests/unit/python/test_cross_language_participant_id_drift.py
  - docs/issues/ISSUE-0094-everyone-broadcast-rejected-by-agent-inbound-validation.md
  - docs/issues/ISSUE-0275-old-messages-from-a-loose-id-still-reach-agents.md
  - docs/issues/ISSUE-0276-cli-accepts-a-one-character-id.md
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

> 2026-11-16 — resolved by [#1136](https://github.com/mkhomutov/Persatrix/pull/1136), on the maintainer's two
> choices.
>
> **The strict rule everywhere.** The channels package and the four places
> of the channel schema now hold `^[a-z0-9][a-z0-9-]*[a-z0-9]$`, as the
> agents, the accounts package and the agent schema did. A drift test
> compares the copies, nine at this point and ten since the review (the
> note below)
> (`tests/unit/python/test_cross_language_participant_id_drift.py`). The
> orchestrator refuses an id that does not fit where it enters: a
> publish's sender and mentions, a new member, a new channel's members
> and a new DM answer `400`, with the id and the rule in the message, and
> `make validate` refuses the id in `channels.yaml`.
>
> **Members already stored keep loading.** The looser rule is kept under
> one name, `legacyParticipantIDPattern`, for the two paths that only read
> what may be stored: the config loader, whose channel may already be in
> the store, and the DM address, which a history read derives. A store
> that holds such members logs one warning when it opens, naming each.
> They can be listed, read and removed, and cannot post, be mentioned or
> be added again.
>
> Two things this text had wrong or left out. The refusal was not
> wordless on the orchestrator's side: each persona's refusal is logged
> there as a `channels: dispatch failed` warning. It was wordless for the
> member, who got `201`. And the CLI does not hold exactly the strict
> rule: it lets a one-character id through
> ([ISSUE-0276](ISSUE-0276-cli-accepts-a-one-character-id.md)).
>
> **Where the looser rule came from, and what the strict one takes
> away.** The first channels pull request (RFC 0011 PR 1, pull request
> 231, April 2026) set the rule "ASCII only, no colon, no whitespace",
> and its review made the schema, the loader and the store hold that one
> pattern. The comment and the glossary gave the reason: a person who is
> not an agent, such as a CLI user, should be able to keep the casing of
> a name from elsewhere (`User_1`). A later review (pull request 609)
> widened `escalation_chair_id` to the same shape for the same reason.
> The agents never held that rule, so the purpose was never met: such a
> person could post and was never answered. The CLI never sent such an
> id either: it lowercases the operating system's user name and turns
> every other character into a hyphen. The strict rule takes away the
> promise itself. A person's id can no longer carry a capital, an
> underscore, a single character or a hyphen at either end, on any
> route.
>
> **What an old row still does.** A message stored under a loose id
> stays in its channel's history. It reaches an agent in two ways, and
> closing them needs more than refusing at the door and warning at
> start, so they are left for a decision:
> [ISSUE-0275](ISSUE-0275-old-messages-from-a-loose-id-still-reach-agents.md).

> 2026-11-16 — decided for the thread, the first of the two: it is left
> as it is. [ISSUE-0275](ISSUE-0275-old-messages-from-a-loose-id-still-reach-agents.md)
> records the decision and stays open as the known gap.

> 2026-11-16 — changes after the review of pull request 1136, on the
> maintainer's decisions.
>
> **Two more doors.** A settings edit could still write an id outside
> the rule as a channel's escalation chair or convener. It now refuses
> one that the edit newly sets, with the rule in the message. A value
> the channel already holds is left alone, so a channel whose stored
> settings name such a member stays editable and can be pointed at
> another member. And the web console's composer turned `@User_1` into a
> mention itself and offered the member in its menu, so its send was
> refused; it now leaves such a member as text and out of the menu. The
> console's copy of the rule is the tenth the drift test compares.
>
> **One more reason for the looser rule.** RFC 0011 gave another reason
> beside a person's casing: a tight pattern "would foreclose future ULID
> identifiers". The strict rule does that at the door: a ULID in its
> usual upper-case form is refused, and in lower case it fits. RFC 0043,
> a draft, still quotes the earlier pattern for a channel member. Neither
> RFC is changed here.
>
> **What the review found that this fix leaves open:** the agents'
> checks accept an id that ends in a line break
> ([ISSUE-0280](ISSUE-0280-agents-accept-a-participant-id-that-ends-in-a-line-break.md)),
> the rule sets no length
> ([ISSUE-0281](ISSUE-0281-participant-id-rule-has-no-length-bound.md)),
> and the refusal of an agent's registration names another pattern than
> the one it checks
> ([ISSUE-0282](ISSUE-0282-agent-registration-refusal-names-a-pattern-the-check-does-not-hold.md)).
> The config loader still takes a chair or convener of the earlier shape
> from `channels.yaml`, as it takes such a member: it cannot tell a new
> value from one an earlier start ran with, and `make validate` refuses
> the file.
