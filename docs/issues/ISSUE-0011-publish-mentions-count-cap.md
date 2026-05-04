---
id: ISSUE-0011
summary: handlePublishMessage forwards req.Mentions without per-element or count cap; defense-in-depth gap on unauth REST surface
status: open
severity: low
area: internal/server
created: 2026-05-04
refs:
  - docs/rfcs/0011-channels.md
  - docs/rfcs/0011-pr-plan.md
  - docs/pr-reviews/pr-245-review.md
---

## Summary

`handlePublishMessage` in `internal/server/channel_handlers.go` forwards
`req.Mentions` straight into `msg.Mentions` with no per-element length cap
or per-message count cap. Combined with the v0.3.0 unauthenticated REST
surface, a client can publish a message containing thousands of mentions
(bounded only by the 1 MiB body cap from `decodeJSON`).

## Context

Captured during PR #245 deep review (Should-Fix #3). The agent-side
publish action already enforces a `_MAX_MENTIONS_PER_ACTION` cap; the REST
boundary does not mirror it. PR 4's response gate uses
`agent_id ∈ event.mentions` as a trigger, so unbounded mention lists
become a quadratic-cost vector on the persona-runtime side.

## Impact

- Defense-in-depth gap on the v0.3.0 unauthenticated surface.
- PR 4 response-gate cost scales with `len(mentions)`; an attacker can
  amplify per-publish work without paying for it themselves.
- Already pinned: write-side validation that mentions resolve to real
  participants is deferred to PR 4 (PR #231 SF-3). This issue covers the
  numeric cap, which is independent of resolution.

## Proposed fix / investigation path

1. Introduce a `channelMaxMentionsPerPublish` constant alongside the
   existing `channelMaxLimit` etc. in `internal/server/channel_types.go`
   (or `internal/channels/`). Mirror the agent-side
   `_MAX_MENTIONS_PER_ACTION` value.
2. In `handlePublishMessage`, reject with 400 + structured error if
   `len(req.Mentions) > cap`.
3. Add a unit test covering both the at-cap (accept) and over-cap (400)
   cases.

## Notes

> 2026-05-04 — initial capture during PR #245 review (Should-Fix #3).
