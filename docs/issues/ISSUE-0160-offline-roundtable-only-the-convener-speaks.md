---
id: ISSUE-0160
summary: "make demo-autonomous convened the roundtable and only the convener spoke: the mock provider answered every participant's open-floor bid with its canned discussion paragraph, the gate found no should_post:/speak: line in it and resolved each bid to silence as parse_failure, so ember-owl and iron-fox never posted; the convener re-posted its identical opener at every stall (the advance directive still carries the topic keyword), and the chair's stall escalation matched the synthesis fixture and posted the closing synthesis early, then again at the close. CI stayed green because its offline test fed participants the opener directly, skipping the gate."
status: in_progress
severity: medium
area: persona
created: 2026-09-15
refs:
  - agents/llm_offline.py
  - config/offline_responses.yaml
  - agents/salience_bid.py
  - agents/salience_deliberation.py
  - tests/integration/test_autonomous_offline_smoke.py
  - tests/unit/python/test_llm_offline_bid.py
  - docs/guides/autonomous-channels.md
  - docs/v0.3.16-release-checklist.md
---

# ISSUE-0160: The offline autonomous demo is a monologue — only the convener speaks

## Summary

`make demo-autonomous` boots the `roundtable` on the zero-cost mock provider,
arms it and convenes it. The booted discussion was nine messages: the
convener's opener posted six times word for word, and the chair's closing
synthesis twice. The two participants never spoke. Every part of the arc that
the demo exists to show — a room discussing a topic with no human — was
missing, while the mechanics around it (the convene, the stall cadence, the
bounded close, $0) all worked, which is why the release-prep smoke recorded it
as a pass for five releases.

## Context

Found at the v0.3.16 release-prep final sweep (PR 4) when the maintainer
watched the offline smoke in the web console; the maintainer held the tag for
the fix. Three separate causes, each read off the booted stack:

1. **The bid never parsed.** In a group channel a participant first asks the
   model whether to post at all ([`salience_bid.py`](../../agents/salience_bid.py));
   the answer must carry a `should_post: yes|no` line (`speak: yes|no` in mode
   `off`). The mock returns plain text matched by keyword, and the bid prompt
   contains the channel message, so it returned the persona's canned
   paragraph. The parser found no verdict and resolved the bid to silence:
   iron-fox and ember-owl each logged six `agent.deliberated` records, all
   `should_post: false`, `reason_code: parse_failure`.
2. **The convener repeated itself.** A stall hands the convener an
   agenda-advance directive that repeats the topic, and the topic contains
   the opener's keyword, so every advance and re-invite posted the opener.
3. **The synthesis came early.** The chair's stall-escalation framing says
   "synthesis", which matched the closing-synthesis fixture.

The execution reports since v0.3.11 recorded "mock personas cannot bid" as a
known offline shape rather than a finding. The CI face,
`test_autonomous_offline_smoke.py`, asserted that "the participants engage on
topic" by feeding each participant the opener directly — the one step the
booted arc never lets them skip.

## Impact

The one-command offline demo is the project's first impression and the demo
the [Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens)
expects before the first strategy review. It showed a persona talking to an
empty room. No live-provider path was affected: real models answer the bid in
its grammar.

## Fix

The mock provider answers a bid prompt with a verdict in its grammar: *speak*
when the persona has a scripted point for the new message that it has not made
in the window, *silent* with `nothing_to_add` or `already_answered` otherwise;
the catch-all never admits a bid. Every reply picks the first point not yet
made, and a turn handed over without a bid (a reply that @-mentions the
persona, or the floor re-fanning a round's last reply) falls back to the
catch-all once before repeating anything. Because the runtime drops rows newer
than the message being answered, the window can omit the persona's own latest
reply, so "made" replays the persona's deterministic choices over the window
rather than trusting its own posts alone. A reply marked `closes: true` (the
chair's synthesis) ends a discussion, so a re-convened room starts fresh. The
roundtable script gains the convener's agenda-advance lines, the chair's
interim escalation read, and the points each persona needs for the exchange
the booted arc actually takes.

Pinned by `tests/unit/python/test_llm_offline_bid.py` (the real gate, all three
reasoning modes) and the floor class in
`tests/integration/test_autonomous_offline_smoke.py`, which plays the room
through the gate. Booted twice on the fix: opener → both participants → a
three-turn exchange → the chair's synthesis, seven messages, no repeats, the
structural close, 15 reconciled leases at `actualUSD` 0; re-convened on the same
stack, the same seven.

## Notes

> 2026-09-15 — filed and fixed in the same PR. Two observations from the booted
> runs are recorded elsewhere rather than fixed here: a silent reply on a
> handed-over autonomous turn left one booted run open with nothing further
> dispatched ([ISSUE-0161](ISSUE-0161-silent-reply-on-a-handed-over-autonomous-turn-stalls.md)),
> which is why the mock never answers such a turn with silence; and the
> productive arc uses 58 of the agents' shared 60 REST calls per minute, so a
> re-convene inside that minute is refused
> ([ISSUE-0111](ISSUE-0111-anonymous-wallet-rpcs-share-rate-limit-bucket.md), Notes).
