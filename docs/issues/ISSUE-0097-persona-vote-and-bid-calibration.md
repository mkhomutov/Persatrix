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
   two separate messages milliseconds apart. W counts turns: with
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

> 2026-06-13 — **PR 1 (defect 1, opening-round bid).** Calibrated the Tier B
> bid prompt in [`agents/salience_bid.py`](../../agents/salience_bid.py)
> `_build_bid_messages`: an *unanswered direct question put to the room* is
> named as salient on its own — answering it IS the new content, score it
> high — while the redundant case (someone has already answered) still routes
> to silence, so the bias-to-silence posture (TB2) is preserved. Prompt-side,
> no mechanism change. Pinned with `TestOpeningQuestionCalibration` in
> `tests/unit/python/test_salience_bid_prompt.py` (the prompt-construction
> tests split out of `test_salience_bid.py` in this PR). Issue stays **open**:
> the unit test pins the steer, but defect 1's close needs a live
> MT-CHANNEL-GOV-004 run showing an un-mentioned opening round draw replies
> without `--mention` nudges — and the run must include a *single-answer*
> opening question (not only the "name one risk **each**" phrasing this issue
> captured), to confirm the steer does not flip unanimous silence into a
> thundering herd: every persona bids concurrently against the same empty
> transcript, so the "unless someone has already answered" guard cannot fire
> on the opening round and all members may answer at once. Defect 2 (split
> prose+vote → single-message vote in
> [`end-interaction-vote.md`](../../prompts/runtime/safety/end-interaction-vote.md))
> is **PR 2**, not yet started.

> 2026-06-13 — **PR 2 (defect 2, split prose+vote).** Added the single-message
> steer to [`end-interaction-vote.md`](../../prompts/runtime/safety/end-interaction-vote.md):
> whatever travels alongside the vote — agreement, a closing remark, a caveat —
> goes *inside* the vote's `content` as that one message, never as prose first
> and the vote second, because the split arrives as two turns and a concurring
> vote that trails its own prose can land outside the `end_vote_window` that
> closes the discussion (the 04:04:01Z miss this issue captured: prose at .593,
> vote at .600, distance 3 at `end_vote_window: 3` — `state.turn - voteTurn < w`
> fails `3 < 3`, so the chair's vote is out of window). The steer is **not**
> narrowed to agreement: the miss is caused by any extra turn between the two
> votes, not by concurrence specifically. It ports the precise clause the
> [`chair-escalation.md`](../../prompts/runtime/safety/chair-escalation.md)
> snippet already gives the chair (ISSUE-0098) — *prose beside the action block
> does not travel inside your vote, it reaches the room as a separate,
> disconnected message* — rather than the ambiguous "one message together"
> summary the first cut used, which could be misread as "prose + action block
> in one turn" (the very split the chair clause warns against). The snippet's
> own opening line was reconciled at the same time: *instead of (or **folded
> into**) a final reply*, no longer *(or after)*, which had blessed the exact
> reply-then-separate-vote split this paragraph forbids. Prompt-side, no
> mechanism change. TDD:
> `test_snippet_steers_what_you_say_into_the_vote_not_a_separate_message` in
> `tests/unit/python/test_end_interaction_vote_action.py` (rewritten red first
> to pin co-location, the ported clause, the polarity-explicit *one message,
> not two*, and the generalised scope — the prior version asserted a tautology
> (`content` is named several times anyway) and bare tokens an inverted steer
> would still satisfy), plus the
> `tests/unit/python/test_persona_section_composer.py` byte-identity golden
> updated for the rewritten paragraph and the reconciled opening line. Issue
> stays **open**: defect 2's close needs a live MT-CHANNEL-GOV-004 run where a
> concurring persona casts a single-message vote and the quorum lands
> (`trigger=end_votes`), without the "reply with just your vote, no preamble"
> operator nudge the original run needed.
