---
id: ISSUE-0096
summary: "Persona-authored display-name @-mentions (\"@Iron Fox\") resolve to no member; chair hand-offs silently restart nothing"
status: resolved
resolution: "Closed by the display-name-mention-lifting amendment (#617 amendment+acceptance, #618 resolver, #619 publish-seam wiring): the REST publish handler now lifts in-text `@`-mentions (membership-scoped, registry display names via `Server.liftContentMentions` → `channels.LiftDisplayNameMentions`) and unions the canonical ids into `mentions` before persist and fanout. Verified live 2026-06-13 (main @ def19ca): a prose `@Ember Owl` with no `--mention` woke `ember-owl` (a when_mentioned member that stayed silent pre-fix), persisted `mentions=[\"ember-owl\"]`, and logged `lifted=[\"ember-owl\"]` — see the MT-CHANNEL-GOV-004 Test Results row."
closed: 2026-06-13
closed_pr: 619
severity: medium
area: channels
created: 2026-06-12
refs:
  - docs/rfcs/0011-amendment-display-name-mention-lifting.md
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
turn named members by display name (`@Iron Fox`) — the spelling the
channel roster section surfaces to the persona
([`channel_roster.py`](../../agents/persona_runtime/channel_roster.py)),
not the participant id the message stream renders each speaker by
(`Message from iron-fox:`). The orchestrator logged
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
> completeness-fixation) is written up in
> [ISSUE-0098](ISSUE-0098-chair-completeness-fixation-blocks-synthesis.md);
> the spent-ration half is
> [ISSUE-0099](ISSUE-0099-ce5-ration-spent-on-provably-failed-handoff.md).
> This resolver fix is the highest-leverage link.

> 2026-06-13 — fix workstream opened: the
> [display-name-mention-lifting amendment](../rfcs/0011-amendment-display-name-mention-lifting.md)
> (PR 1/3, amendment + skip-guarded acceptance). The full trace moved the
> seam from this issue's original proposal: `resolveFloorMentions` alone
> cannot fix it — multi-word display names are rejected per-mention at the
> store before ever reaching the resolver, and in all three live
> reproductions the names existed only in *content* (the chair's hand-off
> is a prose reply, so its structured mentions are the synthesized inbound
> sender — the `respond: never` human). The fix is membership-scoped
> lifting at the REST publish seam, unioned into `mentions` as canonical
> ids before persist and fanout; the resolver and both gates stay
> untouched and start seeing the ids the prose always meant.

> 2026-06-13 — fix landed (PR 3/3): the publish handler now lifts
> in-text `@`-mentions (`Server.liftContentMentions` →
> `channels.LiftDisplayNameMentions`) and unions the canonical ids into
> `mentions` before persist and fanout; the skip-guarded acceptance is
> unskipped and green. Status stays `in_progress` until the live
> MT-CHANNEL-GOV-004 Edge Case 1 re-run confirms a display-name hand-off
> actually restarts the discussion (the amendment's §C item 3
> acceptance) — that re-run closes this issue.
