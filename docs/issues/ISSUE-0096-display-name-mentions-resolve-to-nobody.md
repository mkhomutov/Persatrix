---
id: ISSUE-0096
summary: "Persona-authored display-name @-mentions (\"@Iron Fox\") resolve to no member; chair hand-offs silently restart nothing"
status: open
severity: medium
area: channels
created: 2026-06-12
refs:
  - docs/rfcs/0030-amendment-chair-stall-escalation.md
  - docs/rfcs/0030-amendment-floor-capable-directedness.md
  - docs/manual-tests/MT-CHANNEL-GOV-004.md
---

## Summary

Personas naturally @-mention each other by display name — the live
MT-CHANNEL-GOV-004 chair hand-off (Edge Case 1, outcome b) opened with
`@Ember Owl @Iron Fox — alex needs one risk each from all of us…`. The
mention parser yields no member match for space-separated display names
(member ids are `ember-owl` / `iron-fox`), so
`resolveFloorMentions` found no floor-capable member and the publish was
reclassified to open floor ([`fanout.go`](../../internal/channels/fanout.go)
~line 78, the directedness amendment's gate flip). Open floor means Tier B
bids — which had just unanimously passed — so the hand-off drew silence
and the discussion died, defeating the escalation's outcome (b) entirely.

## Context

Observed live on 2026-06-12 (build main @ 113c728): the chair's forced
turn named members exactly as the conversation window renders them
(`**Iron Fox:**` headers), the orchestrator logged
`channels: mentions name no floor-capable member`, and iron-fox's
open-floor bid passed on a message that explicitly asked it to speak.
The directedness amendment's debug line surfaced the failure precisely
— the resolution gap is upstream, in what counts as a mention match.

## Impact

The chair-stall-escalation amendment's outcome (b) ("name the member
best placed and ask them directly — the named member's reply restarts
the discussion") is unreliable whenever the chair writes names the way
its own prompt context displays them. More broadly, any persona-to-
persona directed address by display name falls back to the
salience-gated open floor, which under conservative bid calibration
means silence.

## Proposed fix / investigation path

Resolve mentions against member **display names** as well as ids, in
the channel-membership-scoped resolver (`resolveFloorMentions` /
the mention extraction it consumes — `floor_mentions.go`): case-
insensitive, multi-word (greedy longest-match against the channel's
member set, so `@Iron Fox` matches member `iron-fox` whose display name
is "Iron Fox"). Membership-scoped matching keeps it deterministic and
collision-bounded (a channel with two members displaying the same name
is a config smell the loader can warn on). Prompt-side steering ("always
mention by id") is NOT the fix — it fights how the conversation window
itself renders speakers to the model.

## Notes

> 2026-06-12 — initial capture during the MT-CHANNEL-GOV-004 live run;
> the hand-off that exercised it is in `group:planning` history at
> 02:57:20Z.

> 2026-06-12 (second session, build main @ d47385d) — reproduced twice
> more: both chair forced turns handed off to "@Ember Owl" / "Ember" by
> display name and died into silence, deadlocking both escalated
> interactions to idle. The full cycle (this issue + the chair's
> completeness-fixation) is written up in [ISSUE-0098]; the spent-ration
> half is [ISSUE-0099]. This resolver fix is the highest-leverage link.
