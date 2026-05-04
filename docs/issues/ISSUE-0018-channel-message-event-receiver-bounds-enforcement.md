---
id: ISSUE-0018
summary: ChannelMessageEvent wire bounds (content/mentions/thread_id/channel_type) are documented as receiver MUSTs only in proto comments — no schema-level enforcement
status: open
severity: medium
area: agents
created: 2026-05-04
refs:
  - proto/task.proto
  - docs/rfcs/0011-channels-bridges.md
  - docs/rfcs/0011-pr-plan.md
  - docs/issues/ISSUE-0011-publish-mentions-count-cap.md
---

## Summary

`ChannelMessageEvent` (added in PR #246) documents four hard limits as
receiver MUSTs in proto comments only:

- `content`: max 4000 chars
- `mentions[]`: max 10 entries
- `thread_id`: max 128 chars
- `channel_type`: must be one of `{group, dm, thread}` and must match
  the `channel_id` prefix

proto3 has no native length / enum-membership constraint, and the PR 3
stub in `agents/server_servicers.py` returns
`TaskAck(success=False, …)` without inspecting the request. PR 4's real
handler is therefore the single point where the contract is enforced or
quietly broken.

## Context

Captured during PR #246 deep review (Should-fix #2). Companion to
[ISSUE-0011](ISSUE-0011-publish-mentions-count-cap.md), which already
tracks the publish-side mention cap on the REST surface; this ticket
covers the receive-side cap inside `AgentServiceServicer.ReceiveChannelMessage`
once the real handler lands in RFC 0011 PR 4.

## Impact

- Without enforcement, an oversized `content` or unbounded `mentions[]`
  reaches downstream persona logic, memory writes, and audit logs
  unchecked. The trust-boundary annotation on
  `ChannelMessageEvent.sender_id` (Low risk in v0.3.0 cleartext gRPC,
  rising in v0.5.0) compounds the exposure.
- An unknown `channel_type` value (4th-value drift) silently routes to
  the default arm of every receiver-side switch.
- Future receivers in other languages (Rust CLI, Go ad-hoc tooling)
  would each have to re-implement the same four checks; the contract
  drifts.

## Proposed fix / investigation path

1. In RFC 0011 PR 4, add a single validation function in
   `agents/server_servicers.py` (or a shared helper under
   `agents/dispatch.py`) that enforces all four bounds and returns
   `TaskAck(success=False, error_message="…")` on violation. Rejected
   messages should also increment a labeled metric (`reason="content_too_long"`,
   `reason="mentions_overflow"`, `reason="bad_channel_type"`,
   `reason="thread_id_too_long"`) for ack-rate dashboards.
2. Pin each bound with a unit test in `tests/unit/python/` —
   table-driven, one row per bound, asserting `success=False` and the
   metric label.
3. Consider promoting `channel_type` to a proto enum (tracked
   separately under ISSUE-0020) so the membership check collapses to a
   single language-agnostic constraint.
4. Once PR 4 ships, update this ticket to status `resolved` with the
   closing PR.

## Notes

> 2026-05-04 — initial capture during PR #246 deep review (Should-fix
> #2). Largest residual risk in PR 3's scope per the review's Security
> Assessment section.
