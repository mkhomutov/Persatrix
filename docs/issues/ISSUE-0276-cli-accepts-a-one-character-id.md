---
id: ISSUE-0276
summary: "The CLI's id check says it holds the pattern of an agent id, and lets a one-character id such as `a` through, which that pattern refuses. A person whose operating-system user name is one character long therefore sends an id the orchestrator answers with 400 on every `chat` and `channel send`, unless they pass another id."
status: open
severity: low
area: cli
created: 2026-11-16
refs:
  - cli/src/validation.rs
  - cli/src/user_id.rs
  - cli/src/commands/channel.rs
  - cli/src/commands/channel_tests.rs
  - internal/channels/identifiers.go
  - docs/issues/ISSUE-0209-participant-id-rule-differs-between-go-and-python.md
  - https://github.com/mkhomutov/Persatrix/pull/1136
---

# ISSUE-0276: The CLI accepts a one-character id

## Summary

`validate_resource_id` in `cli/src/validation.rs` is documented as
matching `^[a-z0-9][a-z0-9-]*[a-z0-9]$`. That pattern needs two
characters. The function checks the last character and the middle ones
only when the value is longer than one, so `a` passes, and its own unit
test asserts that it does. Every other id the function accepts fits the
pattern, and every id it refuses is one the pattern refuses.

## Context

The function guards agent ids, workflow ids, channel names, member ids,
the sender of `channel send`, its mentions and the user id of `chat`.
When no user id is given, the CLI derives one from the operating
system's user name (`normalize_user_id` in `cli/src/user_id.rs`):
lowercase, every other character turned into a hyphen, hyphens trimmed
from both ends. A one-character user name stays one character.

Until
[ISSUE-0209](ISSUE-0209-participant-id-rule-differs-between-go-and-python.md)
was fixed the orchestrator took a one-character participant id, stored
the message, and every persona refused it without a word to the sender.
It now answers `400` with the rule in the message, so the person is told
why, and `--as` or `--user` with a longer id works.

Read, not run: the CLI is not changed while its freeze holds, and it
was compared with the rule by reading the function and its tests.

Three comments in the CLI describe the orchestrator's rule as it was
before that fix, and wait for the same freeze: the doc comment of
`validate_send_inputs` in `cli/src/commands/channel.rs` ("The server's
`participantIDPattern` is broader"), and two in
`cli/src/commands/channel_tests.rs` ("Server's participantIDPattern
accepts uppercase", "Underscores pass the server regex"). The
orchestrator now holds the rule the CLI holds, so none of the three is
true any more. The tests under them still assert the right thing.

## Impact

Small. A one-character user name, or a one-character id typed on
purpose, gets a clear refusal from the orchestrator where the CLI could
have given its own before sending.

## Proposed fix / investigation path

Refuse a value shorter than two characters in `validate_resource_id`,
change the unit test that accepts `a`, and have `normalize_user_id`
fall back to `local` for a one-character result, as it does for an
empty one. Correct the three comments in the same change.

## Notes

> 2026-11-16 — found while checking the CLI against the participant id
> rule for
> [ISSUE-0209](ISSUE-0209-participant-id-rule-differs-between-go-and-python.md)
> ([#1136](https://github.com/mkhomutov/Persatrix/pull/1136)).
> Waits for the CLI freeze to end. Severity proposed (low); not ruled by
> the maintainer.

> 2026-11-16 — the three stale comments were added to this issue after
> the review of pull request 1136 found them.
