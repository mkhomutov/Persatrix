---
id: ISSUE-0112
summary: "RFC 0052 — a duplicate in-window end-vote's fanout suppression wedged an autonomous discussion open forever: the dedup branch (end_vote.go processEndVote) suppressed dispatch AND the fanout tail, so when the duplicate was the last publish in flight no round ever minted again — the stall tail, `max_rounds` tally, and idle rotation (lazy, next-publish) all unreachable. The ISSUE-0110 sibling on the vote-dedup suppression branch. FIXED: an armed channel re-fans the duplicate (quorum dedup unchanged; §D bounds terminate the re-fanned chain); human channels keep the suppression byte-for-byte."
status: resolved
severity: high
area: channels
created: 2026-07-21
closed: 2026-07-21
refs:
  - docs/rfcs/0052-autonomous-agent-channels.md
  - docs/issues/ISSUE-0110-autonomous-productive-round-continuation-stall.md
  - internal/channels/end_vote.go
  - docs/manual-tests/v0.3.11-execution-report.md
---

## Summary

The third fanout-suppressing branch without an RFC 0052 disposition. The
Layer 4 end-vote hook dedupes a redundant in-window duplicate vote and
suppresses its fanout (an anti-flood hardening — votes are reply-budget-exempt,
so re-fanning duplicates would be a budget bypass). On a human channel that is
correct: the human continues the conversation. On an `autonomous.enabled`
channel, when the duplicate is the **last publish in flight**, the suppression
is terminal: no dispatch and no fanout tail means **no round ever mints
again**, so the stall tail cannot escalate, the `max_rounds` tally cannot
advance, and idle rotation (lazily evaluated on the next publish) never runs —
the interaction wedges open forever with no synthesis. §D's "terminates
deterministically" violated by the roster's most natural convergence shape:
personas voting "we're done" below quorum.

## Live reproduction (2026-07-21, v0.3.11 release-prep MT-AUTONOMOUS-002)

3-persona roster (`nova-sparrow` convener, `iron-fox` chair, `ember-owl`),
`end_vote_threshold=3`, live Anthropic. The convener cadence walked the agenda
(two clean `advance` events), the roster converged, `ember-owl` and `iron-fox`
each voted; `nova-sparrow` (busy issuing follow-ups) never did — 2 of 3, no
quorum. The final message was ember's follow-up vote: deduped
(`duplicate end-of-interaction vote` WARN), fanout suppressed, and the channel
went silent with the interaction **stuck open 40+ minutes** (re-convene 409
"already has an open interaction") until operator action.

## Fix

`processEndVote`'s spam branch resolves the channel's autonomous config: an
armed channel **re-fans** the duplicate — still a no-op for the quorum, and
the re-fanned (typically all-silent) round advances the round tally toward
`max_rounds`, so the §D bounds terminate the chain exactly as designed; the
flood the suppression prevents is impossible under those bounds. The
`governance_drop{layer=end_vote}` counter now records at the actual
suppression site only (nothing is dropped on the armed path). Human channels
byte-for-byte unchanged. TDD:
`TestEndVote_DuplicateLiveVoteFansOutOnAutonomousChannel` (red pre-fix) +
the pre-existing suppression/spam/stale-revote suite green.

## Notes

> 2026-07-21 — found and fixed during the v0.3.11 release-prep live MT sweep
> (execution report F-3). Pattern note for future suppression branches: every
> `publishCommit`/`processEndVote` early return that suppresses fanout needs an
> explicit autonomous-channel disposition (continue, close, or re-fan) — the
> ISSUE-0110 floor-suppression fix, the cascade-cap close, and this are the
> three instances of the same class.
