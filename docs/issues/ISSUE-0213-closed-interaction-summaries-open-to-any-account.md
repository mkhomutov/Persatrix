---
id: ISSUE-0213
summary: "Under `auth.mode: enabled` any signed-in account may read a persona's closed-interaction summaries, and the query filters by persona only. A `user` account would see summaries of other people's DMs and of restricted channels. Latent today, because no production path creates a `user` account yet."
status: open
severity: medium
area: security
created: 2026-11-10
refs:
  - internal/server/auth_policy.go
  - agents/closed_interactions_read.py
  - agents/memory/episodic_closed.py
  - docs/rfcs/0039-user-accounts-authentication.md
  - docs/v0.4.0-plan.md
---

# ISSUE-0213: Closed-interaction summaries are served to any signed-in account

## Summary

`GET /api/v1/agents/{id}/interactions/closed` is registered as
`policyAuthenticated` in `internal/server/auth_policy.go`. The read
behind it (`agents/memory/episodic_closed.py`) selects episodes by
`agent_id`, with optional `scope` and interaction-id filters. It does not
filter by principal, session, source channel or protection level, though
the episode rows carry all four.

## Context

The sibling routes are tighter:

- `POST /api/v1/personas/{participant_id}/recall` is `policyOperator`.
- Chat history is served only for the caller's own verified identity.

Account roles are `operator` and `user` (`internal/accounts`). Nothing in
production code creates an account with the `user` role today: account
administration is RFC 0039 Phase 3, which the v0.4.0 plan lists as
cuttable. So the gap opens on the day a `user` account can exist.

## Impact

Once `user` accounts exist, one person can read the persona's summaries
of conversations with other people, and of channels classified above
what that person may see, with the participants named.

## Proposed fix / investigation path

Make the route `policyOperator` now, which costs nothing while only
operators exist. Before account administration ships, either keep it
operator-only or filter the read by the caller's principal and by
protection level.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading the route policy and the query.
