---
id: ISSUE-0097
summary: "Tier B bids pass on direct opening questions; split prose+vote replies burn a W-window turn — live calibration findings"
status: open
severity: medium
area: persona
created: 2026-06-12
refs:
  - docs/rfcs/0030-amendment-relevance-gated-response.md
  - docs/manual-tests/MT-CHANNEL-GOV-004.md
  - prompts/runtime/safety/end-interaction-vote.md
---

## Summary

Two persona-calibration defects from the 2026-06-12 MT-CHANNEL-GOV-004
live run (Claude provider, demo personas):

1. **Opening-round bid passes.** Un-mentioned direct questions ("Name
   exactly one risk each…") repeatedly drew unanimous Tier B passes on
   the *first* round — the discussion stalled before it existed. Every
   reliable round in the session needed member-id `--mention`s (the
   directed lane bypasses the bid). The bid's "do I have something new
   to add?" framing appears to score an unanswered direct question as
   nothing-new.
2. **Split prose+vote concurrence.** Asked to confirm and vote, a
   persona published agreement prose and the `end_interaction_vote` as
   two separate messages seconds apart. W counts turns: with
   `end_vote_window: 3`, the split pushed the concurring vote to
   distance 3 from the chair's vote and the quorum missed — the
   escalated interaction then idled out instead of closing on votes. A
   later run with explicit "reply with just your vote, no preamble"
   steering produced a single-message vote and the close landed.

## Context

Session-long pattern, not a one-off: runs 1–3 all stalled on the
opening round without mentions; the W-window miss is in `group:planning`
history at 04:04:01Z (prose at .593, vote at .600). MT-CHANNEL-GOV-004's
Edge Case 2 anticipates vote-shape disobedience as prompt-calibration
signal; this issue is that signal, captured.

## Impact

(1) makes any un-mentioned group prompt likely to stall, which both
overworks the chair-escalation path (it fires on round one, before
discussion) and makes MT-grade conversations operator-unfriendly.
(2) makes the Layer 4 quorum fragile exactly at concurrence time —
the highest-value vote in the convergence arc — and each miss costs an
idle window of wall-clock before the room is usable for a retry.

## Proposed fix / investigation path

Prompt-side, no mechanism changes:

- Tier B bid snippet: an explicitly *unanswered direct question to the
  room* is salient even when you have no novel argument — answering it
  IS the new content. Calibrate against the opening-round transcript
  from this run.
- [`end-interaction-vote.md`](../../prompts/runtime/safety/end-interaction-vote.md):
  agreement travels INSIDE the vote's `content`, one message — never
  prose first, vote second (the chair-escalation snippet already says
  this for the chair; the base vote snippet needs the same line).

Threshold tuning (`config/channels.yaml` dispositions) is the fallback
if snippet steering proves insufficient; prefer the prompt fix first
(it is provider-portable and does not loosen the suppression posture).

## Notes

> 2026-06-12 — initial capture during the MT-CHANNEL-GOV-004 live run
> (build main @ 113c728).
