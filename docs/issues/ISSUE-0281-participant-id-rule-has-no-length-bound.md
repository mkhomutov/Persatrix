---
id: ISSUE-0281
summary: "The participant id rule sets no length. A person's id of 300 characters that fits the rule is taken: a DM is created and the message stored, the persona refuses the event because the DM's address is longer than the 256 characters it accepts, and the caller waits for a reply that never comes. An agent id is bounded at 256 characters at registration; a person's id is not bounded at all."
status: open
severity: low
area: channels
created: 2026-11-16
refs:
  - internal/channels/identifiers.go
  - internal/channels/legacy_participant_ids.go
  - agents/channel_validation.py
  - internal/server/agent_handlers.go
  - docs/issues/ISSUE-0209-participant-id-rule-differs-between-go-and-python.md
  - https://github.com/mkhomutov/Persatrix/pull/1136
---

# ISSUE-0281: The participant id rule has no length bound

## Summary

`^[a-z0-9][a-z0-9-]*[a-z0-9]$` says which characters an id may hold and
not how many. The orchestrator therefore takes an id of any length. The
agents bound a channel's address at 256 characters, and a DM's address
is made of its two ids, so a long id gives a DM that the store accepts
and no persona will read.

## Context

Run against the store and the agents' check, with an id of 300 lowercase
letters:

- `ValidateParticipantID` accepts it.
- `GetOrCreateDM` with an agent creates the DM, whose address is 313
  characters long, and a message in it is stored.
- The agents' event check refuses that message:
  `channel_id exceeds 256 characters (got 313)`.
- In a group channel the same sender is accepted by both sides: the
  agents bound the channel's address and not the sender.

Over the chat route that is a stored message, a refusal by the persona,
and a caller who gets `504` after the reply wait. That last step was run
by the review of pull request 1136 against a real orchestrator, not by
this file's author.

The same holds before the fix for
[ISSUE-0209](ISSUE-0209-participant-id-rule-differs-between-go-and-python.md):
the earlier rule set no length either.

An agent id is bounded: registration refuses one longer than 256
characters (`MaxAgentIDLen` in `internal/security/middleware.go`).
Nothing bounds the id of a person. Even the agents' bound is longer than
a DM's address may be: two ids of 256 characters do not fit in 256.

One effect is closed in the same pull request. The start-up warning that
names stored members whose id the rule refuses used to show each id
whole, so one stored id of 200,000 characters made a line of 200 kB. It
now shows the first 64 bytes of a long id.

## Impact

Small. It takes an id of some 250 characters, which no client of the
repository produces. The person is not told why nobody answers.

## Proposed fix / investigation path

Give the rule a length, the same on every side, and refuse a longer id
where it enters, with the bound in the message. The agents' 256 for a
channel's address sets the ceiling: two ids and `dm:` and a colon must
fit, so at most 126 characters for one id, which is below the 256 an
agent id may have today. A store can already hold a longer id, so the
read paths keep taking what is stored, as they do for the earlier
shape. The drift test that compares the copies of the rule
(`tests/unit/python/test_cross_language_participant_id_drift.py`) is
where the bound would be pinned.

## Notes

> 2026-11-16 — found by the review of pull request 1136. Severity
> proposed from the tracker's precedent
> ([ISSUE-0276](ISSUE-0276-cli-accepts-a-one-character-id.md), low: an
> id of an unusual size that is taken and then answered by nobody); not
> ruled by the maintainer.
