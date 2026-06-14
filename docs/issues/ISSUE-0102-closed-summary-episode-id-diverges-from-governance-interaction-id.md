---
id: ISSUE-0102
summary: "The agent closed-interaction summary surfaces the persona's RFC 0020 episode id, which can diverge from the orchestrator's RFC 0030 governance interaction id (one governance interaction → several agent-side episode ids); the surface gives no signal the two are different namespaces, so cross-referencing an end-vote-closed id against `agent interactions` finds nothing"
status: in_progress
severity: low
area: agents/persona_runtime
created: 2026-06-14
refs:
  - docs/rfcs/0020-interaction-lifecycle.md
  - docs/rfcs/0030-interaction-id-producer-pr-plan.md
  - docs/manual-tests/MT-CHANNEL-GOV-004.md
  - docs/issues/ISSUE-0098-chair-completeness-fixation-blocks-synthesis.md
  - docs/guides/channels.md
---

## Summary

The `agent interactions <id>` / `GET /api/v1/agents/{id}/interactions/closed`
surface reports an `interaction_id` produced **agent-side** — the persona's
own RFC 0020 idle-segmented memory episode
([`interactions_handler.go`](../../internal/server/interactions_handler.go)
projects `it.GetInteractionId()` straight from the persona's
`GetClosedInteractions` gRPC; the server only proxies it). That id is a
different namespace from the orchestrator's RFC 0030 **governance**
interaction id — the one stamped on channel messages, surfaced in the
escalation/close logs, and used for the end-vote quorum. The two producers
segment independently, so a single governance interaction can map to several
agent-side episode ids, and nothing on the surface signals that the columns
are different id spaces.

## Context

Observed live during the 2026-06-13 MT-CHANNEL-GOV-004 PR-622 PASS run
(main @ 3cde982). The arc closed on the governance interaction
`4b332af1` (`trigger=end_votes`, the id carried on the messages and the
`interaction closed by end-of-interaction votes` log). But the chair's
agent-facing closed summary listed episode ids `0d2ca73d` and `3eb8c3e5`
— neither equal to `4b332af1`. The summaries themselves were correct (they
recorded the right synthesis/resolution); only the *id* used to address
them diverged. Surfaced as a "sibling observation" in
[ISSUE-0098](ISSUE-0098-chair-completeness-fixation-blocks-synthesis.md)'s
resolution and left for a separate look — this is that issue.

The divergence is structural, not a one-off: the agent-side tracker
(`episode_routing.py` / `record_close.py`, RFC 0020 idle boundaries) and
the orchestrator's interaction-id producer (RFC 0030) are different
mechanisms with different idle clocks, so they need not agree on where one
interaction ends and the next begins. A governance interaction that spans
an agent-side idle boundary splits into ≥2 episodes; an agent-side episode
that spans a governance idle/vote close merges what governance saw as two.

## Impact

Operator/observability, not correctness — the summaries are accurate and
the governance close is sound. But the two id spaces share a field name
(`interaction_id`) and look interchangeable, so the natural diagnostic
move — take the end-vote-closed id from the logs and look it up with
`agent interactions --interaction-id <id>` — returns nothing, with no hint
why. It cost real confusion mid-MT (the operator could not correlate the
recorded synthesis back to the interaction that produced it), and any
tooling that joins the two surfaces on id will silently mismatch.

## Proposed fix / investigation path

Disambiguate the namespaces rather than force them to agree (forcing the
agent-side tracker to adopt the governance id is a much larger RFC 0020 ↔
0030 alignment question — out of scope here):

1. **Label the surface.** Rename/annotate the agent-side field as the
   memory-episode id (e.g. `episode_id`, or document `interaction_id` here
   as "persona-memory episode, not the channel governance interaction") in
   the DTO + CLI render, so the two are visibly distinct.
2. **Carry the governance id alongside.** If the persona stamps the
   originating governance `interaction_id` into the episode at close
   (it already receives it on inbound channel messages, CE6 lease
   attribution), the summary could expose both — `episode_id` +
   `governance_interaction_id` — making the end-vote-closed id directly
   look-up-able.
3. At minimum, a `docs/guides/channels.md` note that the two id spaces
   exist and why `agent interactions --interaction-id <governance-id>`
   can miss.

Option 2 is the operator-complete fix; option 1 is the cheap honesty fix.

## Progress

**Cardinality invariant confirmed (settled from code, no live re-run needed).**
The single-column design rests on "one governance interaction → N agent-side
episodes that all carry that *one* id, and no episode spans two governance ids".
This holds by construction: `wire_rotation_closes`
([`interaction_boundary.py`](../../agents/persona_runtime/interaction_boundary.py))
force-closes the open episode whenever the inbound wire id differs from the one
it was opened under (excepting a known-predecessor straggler), and the wire id
is stamped exactly once, first-wins
([`episode_routing.py`](../../agents/persona_runtime/episode_routing.py) — `not
interaction.wire_interaction_id`). So `wire_interaction_id` is single-valued per
episode; an idle/structural split inside one governance arc stamps both episodes
the *same* id (the observed `4b332af1 → 0d2ca73d + 3eb8c3e5`). Governance closes
occur only on group scopes (DM/thread never vote-close; thread scopes are
deliberately wire-untracked), exactly where the id is reliably present. The one
accepted residual — a late-delivered predecessor straggler absorbed into the
successor episode — predates this work and does not affect the end-vote-close
lookup (the closed id labels its own episodes).

**PR 1 (this change) — option 2 display half + option 1 honesty + option 3 docs.**
The governance id is now *persisted* and *surfaced*, disambiguating the
namespaces:

- `close_path.py` persists `interaction.wire_interaction_id` into the episode
  context (previously in-memory only) as `governance_interaction_id`.
- `ClosedInteraction` proto gains `governance_interaction_id` (field 9), plumbed
  through `closed_interactions_read.py` → the Go DTO → the CLI render (a dimmed
  `governance:` line, shown only when present). The agent-side `interaction_id`
  is documented in the proto / DTO / CLI as the persona-memory episode id (the
  option-1 honesty fix), without a breaking JSON-key rename.
- `docs/guides/channels.md` documents the two id spaces and that
  `--interaction-id <governance-id>` can currently miss.

**PR 2 (remaining) — the queryable join.** Promote `wire_interaction_id` from
the context blob to a real `episodes` column and extend the read filter to
`AND (interaction_id = ? OR wire_interaction_id = ?)`, so the natural diagnostic
move — paste the end-vote-closed governance id into `agent interactions
--interaction-id` — returns the episodes directly. Until then, the governance id
is visible (PR 1) but not filterable. Closing this issue is gated on PR 2.

## Notes

- Root cause is the two-producer design (RFC 0020 agent memory vs RFC 0030
  governance), which is intentional — personas segment memory on their own
  idle clock and predate the governance producer. This issue is about the
  *surface* not disclosing the gap, not about unifying the producers.
- Cross-check before fixing: confirm whether the split observed
  (`4b332af1` → `0d2ca73d` + `3eb8c3e5`) was an agent-side idle boundary
  inside the arc, or two genuinely separate episodes the summary paginated
  together. The MT row's wall-clock trail (escalation 05:34:27Z → close
  05:51:05Z, ~16.5 min) brushes the 600 s agent-side idle window, which
  would explain a mid-arc episode split — that is the likeliest mechanism
  and worth confirming first.
