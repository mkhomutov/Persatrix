---
id: ISSUE-0130
summary: "On agent startup the RFC 0011 channel catch-up replay re-derives episodes and facts from replayed channel history under the persona's **default** scope (`principal_id='local'`, `session_id='legacy'`), because `_build_replay_event` has no principal to seed — the orchestrator's `messages` table has no principal column, so the emitting tenant is not persisted with the message and exists only on the live gRPC dispatch. The result is that one authenticated person's private content is silently copied into the shared `local` tenant on **every** restart, unbounded, where any unauthenticated caller (the whole persona fleet, and every caller under `auth.mode: disabled`) resolves. The person↔person boundary v0.3.14 promises still holds — a second authenticated principal cannot read it — but the person→anonymous boundary does not. Third ISSUE-0082 residual (R-3); found live at the v0.3.14 MT-MEMORY-MULTIUSER-001 execution run."
status: resolved
severity: high
area: memory
created: 2026-08-18
closed: 2026-08-18
refs:
  - agents/channel_catchup.py
  - agents/persona_runtime/__init__.py
  - agents/persona_runtime/summarize_close.py
  - internal/channels/sqlite_schema.go
  - internal/server/channel_types.go
  - docs/issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md
  - docs/issues/ISSUE-0081-session-id-process-global-not-task-local.md
  - docs/manual-tests/MT-MEMORY-MULTIUSER-001.md
  - docs/manual-tests/v0.3.14-execution-report.md
---

## Summary

The tenant boundary v0.3.14 lands is a **per-turn** property of the live
dispatch. The catch-up replay is not a live dispatch — it is the agent
re-reading the orchestrator's stored history at boot — so it carries no
principal, and every memory it derives lands in the shared tenant.

## Context

`MT-MEMORY-MULTIUSER-001` was executed live on 2026-08-18 (v0.3.14
release-prep PR 1). Legs 1–2 wrote Alice's disclosure and left a clean
surface:

```
episodes       [('alice-person', 2)]
facts          [('alice-person', 2)]
relationships  [('alice-person', 1), ('local', 1)]     # the local row is config-seeded
```

Leg 3 begins with an account rotation, which restarts the orchestrator and
then the personas. Immediately after that restart the same surface read:

```
episodes       [('alice-person', 2), ('bob-person', 1), ('local', 2)]
facts          [('alice-person', 2), ('local', 2)]
```

The two new `local` episodes duplicate Alice's content:

```
{'principal_id': 'local', 'session_id': 'legacy',
 'summary': 'Alice mentioned that her daughter Mira will turn seven next month.'}
{'principal_id': 'local', 'session_id': 'legacy',
 'summary': 'Alice asked for gift suggestions for a child, but the interaction was brief …'}
```

and the two new `local` facts are exact duplicates of Alice's:
`alice-person has_child_named Mira` and `mira has_age seven (next month)`.

Both episodes were written 2 ms apart (`created_at` `1787039961.9951196` /
`1787039961.9971187` — 2026-08-18T07:59:21.995Z), i.e. in one batch, with
**new** `interaction_id`s distinct from the live turns'.

## Diagnosis

The agent log at that instant is unambiguous:

```
07:59:21.960  PERSATRIX_SESSION_ID unset; defaulting to 'legacy' session (RFC 0031 Phase 1 carve-out)
07:59:22.008  channels: catch-up complete agent=ember-owl channels=3 events=6 elapsed_ms=18
07:59:23.412  fact.store … subject=alice-person predicate=has_child_named object=Mira
              source_interaction_id=2beee2b6-de10-4187-849f-3261634b3daf
```

`_build_replay_event` (`agents/channel_catchup.py`) constructs the replay
`AgentEvent` metadata with exactly three seeds — `replay_mode`, the wire
interaction keys (`seed_replay_metadata`), and the RFC 0037 classification
(`seed_channel_classification`). **No principal.** So
`request_scope_from_metadata` binds the persona's default and every
close-path summary and RFC 0026 extraction on the replayed span is written
to `local`.

`replay_mode` short-circuits the *action* loop (no reply is generated), but
not the close-path summariser — the docstring documents running it at boot
as intended behaviour, and the fact extraction rides the same close.

**There is nothing to seed.** The orchestrator's `messages` table
(`internal/channels/sqlite_schema.go`) is:

```sql
CREATE TABLE IF NOT EXISTS messages (
    id, channel_id, sender_id, content, timestamp, thread_id, mentions, metadata
);
```

No principal column, and no principal anywhere else in the channel store.
The emitting tenant exists only as `persatrix-principal` gRPC metadata on
the live dispatch and is discarded at publish time. This is why the gap is
structural rather than a missing line in the replay builder.

## Impact

- **Person → person holds.** Bob (`bob-person`) cannot read the `local`
  rows: recall is strict equality with no carve-out. Leg 3's absence bar
  passed for the right reason, and the release's headline claim survives.
- **Person → anonymous does not.** `local` is what the entire persona
  fleet resolves (RFC 0039 §Non-Goals — agents hold no accounts), what
  every autonomous turn resolves, and what *every* caller resolves under
  `auth.mode: disabled`. Alice's private disclosure is sitting in that
  tenant.
- **It compounds.** Every restart replays the window again and derives
  again: `local` episodes grew `0 → 2 → 5 → 13 → 18` across the arc's first
  four restarts (`local` facts `0 → 2 → 4 → 8 → 10`). The raw series
  continues to 37 and 16, but those later steps are **excluded as evidence**
  — the Leg 5 retag deliberately moved 5 episodes and 2 facts from
  `alice-person` into `local`, and the arc carried extra operator-retry
  restarts. The unbounded *shape* is the finding; no per-restart rate is
  claimed.
- Not observed: an anonymous caller actually being *served* this content.
  The Leg 6 `disabled`-mode turn answered generically. The rows are
  present and in-scope for such a caller; whether recall surfaces them is
  a separate ranking question and was not measured.

## Fix shapes

**(a) Suppress derivation on an unattributed replay** — Python-only, no
migration, no wire change. Skip the close-path summarise/extract for
replayed spans whose principal cannot be established (or write them to a
quarantine tier). Cheap and it stops the leak completely.

The cost is real but bounded: a persona that was *down* when messages
arrived never saw them live, so catch-up is its only chance to derive
memory from them, and this drops that. Note that in the observed case the
derivation was pure **duplication** — Alice's episodes and facts had
already been written correctly under `alice-person` during the live turns
— so for the common restart-with-history case nothing is lost.

**(b) Persist the principal and seed it** — the complete fix. Add
`principal_id` to `messages` (channel store `v11 → v12`), stamp it
server-side at publish from the authenticated request context, surface it
on `channelMessageResponse`, and seed it in `_build_replay_event`.

Note this is **not** blocked by the trust problem that blocks R-2
([ISSUE-0124](ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md), designed in [#822](https://github.com/mkhomutov/Persatrix/pull/822)).
There the principal would have to come from the agent, handing an
unauthenticated caller a cross-tenant *read* primitive. Here the
orchestrator already knows the verified principal at publish time —
`authMiddleware` puts it in the request context — so the value is
server-authoritative and never agent-supplied.

## Resolution — (a) shipped in v0.3.14; (b) is v0.3.15

**Shape (a) landed** as the leak-stopper. `Interaction` gained a
`replayed` marker, captured under the same **frozen-at-open** rule as
`session_id` (the sibling-mislabel guard), and
`persist_closed_interaction` skips derivation entirely for a replayed
span — no episode, so no RFC 0026 extraction, so nothing lands in the
shared tenant.

**The marker alone was not enough**, because replay events *open*
tracker scopes and never close them (RFC 0011 OQ #8, PR-265 review L6's
deferred "lifecycle bleed"): the next live message in the same scope
appends to the catch-up interaction, so the flag would have eaten the
first conversation after **every** restart — the common mid-conversation
case, where the wire interaction id has not rotated and nothing else
splits the span (and the only case for thread scopes, which are
wire-untracked). Verified against the pre-fix behaviour: replay turn +
two live turns + `chat_end` persisted one episode before the marker and
none with the marker alone.

So the marker ships with the boundary the bleed always needed, closing
with the new `REASON_CATCHUP_COMPLETE`:

* **at pass end** — `close_replayed_scopes` pops every replay-opened
  scope when the agent's catch-up pass finishes, in a `finally` so a
  budget overrun closes them too;
* **at ingest** — a LIVE turn arriving on a replay-opened scope splits
  there (`stale_close_reason`, the seam that already owned the wire
  rotation). Needed because the gRPC dispatch surface is already serving
  while catch-up runs, so a live turn can beat the sweep. Replay→replay
  is *not* a split: those turns share one unattributable span, segmented
  by rotation as before.

A conversation interrupted by a restart therefore loses the replayed
span (unattributable) and keeps the live one, derived under its own
principal.

**Two costs, both accepted and stated:**

1. An interaction that opened *and* closed entirely while the agent was
   down is no longer summarised at boot. That path was already
   unreliable — catch-up has no watermark and re-ingests the window every
   boot (RFC 0011 OQ #8), and `Interaction.started_at` is boot time, not
   the wire timestamp.
2. It cannot distinguish *"no principal because the deployment is
   single-tenant"* — where `local` is **correct** — from *"no principal
   because replay lost it"*. Auth mode is not exposed to agents on any
   endpoint. So under `auth.mode: disabled` the skip also fires, where the
   derivation would have been correctly attributed.

**Withdrawn behaviour:** RFC 0037 review item 8 pinned that a replayed
rotation close stamped its episode with the channel classification. There
is no longer a row to stamp, and those tests now pin the skip instead.
Live-path classification capture is untouched.

**Observability:** `agent.interactions.closed.by_catchup_complete` counts
the spans dropped this way — the rate at which restart-window history is
discarded, which is what shape (b) below buys back.

## (b) — required in v0.3.15

Cost 2 above is what **(b)** removes, and it is scheduled, not optional:
persist `principal_id` on `messages` (channel store `v11 → v12`), stamp it
server-side at publish from the authenticated request context, surface it
on `channelMessageResponse`, and seed it in `_build_replay_event`. The
skip then narrows to genuinely unattributable spans, single-tenant
derivation is restored, and RFC 0037's replay stamping returns.

**Correction (2026-08-23): this migration is (b)'s alone, and (b) is not
gated on R-2.** The text here previously read that
[ISSUE-0124](ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md)
(R-2) "needs a column on this same table", so `messages` had to migrate
once for both. R-2's design says otherwise: its chosen shape — server-side
causal attribution, `ChannelRouter.Publish` re-stamping an agent publish
from a per-`(channel, agent)` table — is **in-memory only** and deliberately
so, since a stale attribution is a *mis*-attribution and losing it on
restart is the safer failure. R-2 is self-contained Go with no schema. So
`v11 → v12` carries `principal_id` and nothing else, and (b) can run in
parallel with the R-1/R-2 PRs rather than queueing behind them. The two
still meet at the wire — after R-2 re-stamps, an agent publish reaches the
stamp site with a causal principal on the ctx, so a relayed row persists
that value instead of `local` — but that is a sequencing preference, not a
schema dependency. All three ride the v0.3.15 interaction/tenant train
alongside [ISSUE-0123](ISSUE-0123-per-speaker-interaction-scope.md) (R-1).

## Related

- [ISSUE-0082](ISSUE-0082-orchestrator-per-request-session-principal-emission.md)
  — the emission half; R-1 and R-2 are its first two residuals, this is R-3.
- [ISSUE-0123](ISSUE-0123-per-speaker-interaction-scope.md) (R-1) and [ISSUE-0124](ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md) (R-2) — designed in
  [#822](https://github.com/mkhomutov/Persatrix/pull/822); both re-slotted to
  v0.3.15 so interaction functionality is complete before v0.4.0
  organizations. Their workstream plan is the
  [residuals PR plan](ISSUE-0082-residuals-pr-plan.md); this issue's (b) has
  no PR slot in it and is owned by the v0.3.15 milestone plan.
- [MT-MEMORY-MULTIUSER-001](../manual-tests/MT-MEMORY-MULTIUSER-001.md) —
  does not currently cover this; the MT's restarts *cause* it, and no leg
  reads the `local` partition to check for duplicated content.

## Notes

> 2026-08-19 — shape **(b)** **slotted v0.3.15** by the [sequencing Amendment 2026-08-19](../v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train). It carries the
> channel-store migration on its own: `principal_id` on `messages` is
> `internal/channels/sqlite_schema.go` v11 → v12 (Go). The sibling speaker
> axis ([ISSUE-0131](ISSUE-0131-derived-memory-has-no-speaker-attribution.md))
> does **not** ride it — that lands in the Python persona-memory store
> (migration 17 → 18), a disjoint database. The two are bound by the #822
> Phase 0 **record-shape** decision, not by a shared schema.
