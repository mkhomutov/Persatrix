---
id: ISSUE-0097
summary: "Opening-round bid pass FIXED & live-verified (PR 1); concurrence still splits prose+vote into two messages — defect 2 re-opened (structural, not prompt)"
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

> 2026-06-13 — **Live verification (MT-CHANNEL-GOV-004, build main @ d51f3b4 —
> PR 1 + PR 2 both landed).** Ran the full arc on the Anthropic provider.
>
> **Defect 1 — RESOLVED.** An un-mentioned opener ("Name exactly one risk
> each…") drew open-floor replies with *no* `--mention`: `iron-fox` and
> `nova-sparrow` each posted a distinct risk (`ember-owl` is `addressed`, silent
> by design). Pre-fix this exact round drew unanimous Tier B passes and stalled
> on round one. PR 1's bid calibration is confirmed working live; defect 1
> stands closed.
>
> **Defect 2 — NOT resolved.** On an all-`participant` roster the stall
> escalated to the chair (`nova-sparrow`, interaction `ac4063c4`): Nova
> published a correct **single-message** synthesis-in-vote
> (`end_interaction_vote=true`, the synthesis inside `content`) — the *chair*
> path ([`chair-escalation.md`](../../prompts/runtime/safety/chair-escalation.md))
> is clean. But the *concurrence* path PR 2 targeted still splits. Both drawn
> concurrers — each a *single* `claude-sonnet-4-6` turn — emitted agreement
> prose and the vote as **two separate messages 4–5ms apart**:
>
> - `ember-owl` `9dae857c` (prose, `vote=∅`) @ 10:42:59.786 → `b58d4ecd`
>   (`vote=true`) @ .791
> - `iron-fox` `e2a04ad8` (prose, `vote=∅`) @ 10:43:04.745 → `4084bb9f`
>   (`vote=true`) @ .749
>
> The 4–5ms gap (and one wallet lease per agent) shows this is **one LLM turn
> emitting a free-text block plus the action block**, which the runtime
> persists as two channel messages — not two turns. The PR-2 steer is confirmed
> baked into the running agent image
> ([`end-interaction-vote.md`](../../prompts/runtime/safety/end-interaction-vote.md)
> line 9, *"one message, not two"*) yet did **not** change behaviour. The close
> *did* land (`trigger=end_votes votes=2`, `participant_id=iron-fox`) — but
> **incidentally**: two members were drawn together and their two `vote=true`
> messages fell within W=3 of *each other* (Nova's vote was already out of
> window). With a *single* concurring member, the chair-vote → split-concurrence
> -vote gap reproduces the original out-of-window miss exactly. The defect is
> live.
>
> **Root-cause re-read:** prompt steering is the wrong lever — the split is
> **structural**, a turn's free-text block and its `end_interaction_vote`
> action block persist as two separate channel messages regardless of what the
> snippet says. Defect 2 needs a runtime/serialization change: when a turn
> carries an `end_interaction_vote` action, the sibling free-text message
> should be suppressed (or folded into the vote `content`) so the vote travels
> as one publish. Re-opening defect 2 on that basis. Threshold/window tuning is
> a separate mitigation but does not fix the split.
>
> **Secondary (pre-existing, unchanged):** the chair's synthesis did not draw
> *spontaneous* open-floor concurrence — both members bid-passed on it; the two
> votes had to be `--mention`-drawn. PR 1 calibrated the bid for *unanswered
> direct questions*, not for concurring on a chair synthesis, so this open-floor
> pass-proneness on the convergence turn remains.
