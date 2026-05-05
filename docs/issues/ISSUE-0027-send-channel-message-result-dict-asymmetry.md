---
id: ISSUE-0027
summary: "_handle_send_channel_message returns divergent result dict shapes between REST and legacy branches"
status: open
severity: medium
area: agents
created: 2026-05-05
refs:
  - agents/action_executor.py
---

## Summary

`ActionExecutor._handle_send_channel_message()` returns two different
result dict shapes from a single `action_type`:

- REST branch: `{action_type, status, channel_id}` — no `dispatched_to`.
- Legacy branch: `{action_type, status, dispatched_to}` — no
  `channel_id`.

Downstream consumers (telemetry, reply formatting, evaluators) must
branch on dict shape to read either field.

## Context

Observed during PR #250 review. Both branches were deliberately kept
during PR 4a-ii-β-1 because chat-as-DM lands in β-2 — the asymmetry is
transitional but visible to every downstream now.

## Impact

- Forces every consumer of `_execute_one()` results to write
  shape-defensive code.
- Breaks the implicit "result schema is keyed by `action_type`"
  contract that the rest of `action_executor.py` follows.

## Proposed fix / investigation path

Pick one:

1. **Symmetrize.** Always return both keys, with `None` when not
   applicable: `{action_type, status, channel_id, dispatched_to}`.
2. **Split action types.** Emit `send_channel_message` (REST) and
   `send_channel_message_legacy` (or rename the legacy branch's
   action_type) so consumers dispatch on type, not shape.
3. **Document.** Add an `_execute_one()` docstring contract listing
   per-action-type result schemas. Cheapest, weakest.

Option 1 is the smallest behavioural change and the easiest to test.

## Notes

> 2026-05-05 — initial capture during PR #250 review (Should-Fix #4).
