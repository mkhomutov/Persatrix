---
id: ISSUE-0280
summary: "The agents' two checks of a participant id accept an id that ends in a line break, such as `ab` followed by a new line: Python's `$` also matches before a final line break. The orchestrator and the config check refuse it, so no such id reaches an agent through the orchestrator today."
status: open
severity: low
area: agents
created: 2026-11-16
refs:
  - agents/participant.py
  - agents/channel_validation.py
  - agents/validate.py
  - tests/unit/python/test_cross_language_participant_id_drift.py
  - docs/issues/ISSUE-0154-go-and-python-sanitizers-flag-different-text.md
  - docs/issues/ISSUE-0209-participant-id-rule-differs-between-go-and-python.md
  - https://github.com/mkhomutov/Persatrix/pull/1136
---

# ISSUE-0280: The agents accept a participant id that ends in a line break

## Summary

Every copy of the participant id rule holds the same text,
`^[a-z0-9][a-z0-9-]*[a-z0-9]$`. The readers do not agree on what `$`
means. In Go, in JavaScript and in the repository's config check it is
the end of the text. In Python's `re` it also matches just before a
final line break. The agents' checks use Python's `re` with `.match`, so
they accept `"ab\n"`, which every other reader refuses.

## Context

Run against the code:

| Reader | `"ab\n"` |
|---|---|
| `ValidateParticipantID` (`internal/channels/identifiers.go`) | refused |
| the config check, `agents/validate.py` (what `make validate` runs) | refused: it rewrites `$` to the end of the text |
| `validate_participant_id` (`agents/participant.py`) | accepted |
| `validate_channel_message_event` (`agents/channel_validation.py`), for a sender and for a mention | accepted |

The places are `agents/participant.py` (one `.match`) and
`agents/channel_validation.py` (six: mentions, floor mentions, the
sender, the thread parent's sender, and the same again for a stored
row).

It cannot be reached through the orchestrator: each of its write paths
refuses such an id first, and a Go test holds that
(`TestValidateParticipantID_IsTheOneRule`). It would take a caller that
reaches an agent's own port, or a later path inside the agents that
builds an id without the orchestrator.

This is the fault of
[ISSUE-0154](ISSUE-0154-go-and-python-sanitizers-flag-different-text.md)
in another place: one pattern text, read differently by two engines. The
config check was already corrected for it; the agents' checks were not.

## Impact

None known today. It is a gap between two checks that are meant to be
one rule, in the direction that lets more through on the agents' side.

## Proposed fix / investigation path

Use `fullmatch` in the seven places, or write the pattern's end as
`\Z`, and add the line-break id to the drift test's cases for the
agents' checks. The drift test's docstring names this issue where it
explains why its config cases use the repository's own validator.

## Notes

> 2026-11-16 — found by the review of pull request 1136, which changes
> nothing under `agents/`. Older than that pull request. Severity
> proposed from the tracker's precedent (the same class as
> [ISSUE-0154](ISSUE-0154-go-and-python-sanitizers-flag-different-text.md),
> which is medium because its gap is reachable; this one is not, so
> low); not ruled by the maintainer.
