---
id: ISSUE-0124
summary: "A persona's reply re-enters the orchestrator through `HTTPChannelPublisher` (agents/channel_publisher.py) as a fresh UNAUTHENTICATED REST publish, so every fanout descending from it dispatches under `'local'` even inside an authenticated person's interaction — agent B's restatement of A's disclosure is written to the shared tenant and is recallable by every agent-origin and autonomous turn in any room. The in-process `EventDispatcher` cascade forwards the axis (`origin_principal_id`, agents/action_executor.py); only the REST hop loses it. The persona must NOT send the principal back — it binds `principal_scope` from that header and recall is strict equality, so an agent-supplied claim is a cross-tenant READ primitive. Fix: server-side causal attribution held per `(channel, agent)` at the dispatch chokepoint, ambiguity and expiry failing closed to `'local'`. ISSUE-0082 residual R-2."
status: open
severity: high
area: internal/channels
created: 2026-08-06
refs:
  - docs/issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md
  - docs/issues/ISSUE-0123-per-speaker-interaction-scope.md
  - docs/rfcs/0011-channels-bridges.md
  - docs/rfcs/0039-user-accounts-authentication.md
  - agents/channel_publisher.py
  - agents/action_executor.py
  - internal/channels/grpc_dispatcher.go
  - internal/server/principal.go
---

## Summary

The tenant axis survives every in-process hop and dies on the one hop
that leaves the process and comes back.

## Context

Filed out of the [ISSUE-0082](ISSUE-0082-orchestrator-per-request-session-principal-emission.md)
Part 2 review as residual **R-2**, where it was stated and explicitly
*not* designed. This file carries the design.

v0.3.14 PR 2 threads the RFC 0039 §F verified `participant_id` onto the
request context at `Server.authMiddleware`, and
[`GRPCMessageDispatcher.Dispatch`](../../internal/channels/grpc_dispatcher.go)
emits it as `persatrix-principal` for every dispatch descending from
that request. The predicate is `authIdentity.Authenticated`, and the
persona fleet holds no accounts — it drives the publish/convene REST
seams anonymously per RFC 0039 §Non-Goals. So when a persona replies via
[`HTTPChannelPublisher.publish`](../../agents/channel_publisher.py), the
orchestrator sees a fresh unauthenticated publish and the whole fanout
below it emits nothing.

The asymmetry is worth naming precisely: the **legacy in-process
cascade** does forward the axis — v0.3.13 PR 1 seeds
`EVENT_PRINCIPAL_METADATA_KEY` on child events beside the epoch/session
keys, from `DispatchContext.origin_principal_id`
([`agents/action_executor.py`](../../agents/action_executor.py)). Only
the orchestrator-mediated route loses it.

## Impact

In a multi-agent room, agent B's restatement of A's disclosure is
written under `'local'`. `'local'` is the tenant every agent-origin and
autonomous turn resolves, and facts are cross-room by default
([RFC 0049](../rfcs/0049-memory-consolidation-gradient.md) Phase 1), so
that content can be recalled and spoken into any room the fleet is a
member of — including rooms A is not in. The per-turn boundary holds and
the relayed copy walks around it.

This is not reachable by `MT-MEMORY-MULTIUSER-001`, which drives a
single persona and therefore has no agent-to-agent cascade.

## Proposed fix / investigation path

### The obvious fix is unavailable, and the near-miss is worse

Having the persona echo the principal on its outbound publish would make
the orchestrator trust an agent-supplied identity claim. The persona
binds `principal_scope` from that header and recall is **strict
equality** on it, so an unauthenticated caller could name any principal
and read that tenant. That is a cross-tenant READ primitive traded for a
write leak — strictly worse.

The same objection kills the tempting refinement: correlating the reply
to its stimulus by having the agent echo the stimulus **message id**.
An id is not a principal claim, but an agent sees other members' message
ids in channel history, so echoing a *chosen* id resolves to a *chosen*
principal — the read primitive again, one indirection along. Any
correlation key the agent supplies is disqualified.

### The safe shape: server-side causal attribution

State the orchestrator already knows, held server-side, never accepted
from the wire.

**Where it is written.** `Dispatch` is already the single chokepoint
that resolves session, epoch and principal for a recipient. When
`PrincipalFromContext(ctx)` is non-empty, record

```
(msg.ChannelID, env.Recipient.ParticipantID) → {principal, dispatchedAt}
```

That tuple is exactly the true statement "the orchestrator handed this
agent this stimulus under this principal".

**Where it is read.** In `ChannelRouter.Publish`, before fanout, *iff*
the ctx carries no principal (so an authenticated human publish is never
overridden) and the sender is a registered agent: look up
`(msg.ChannelID, msg.SenderID)` and, on a live unambiguous hit,
`ctx = WithPrincipal(ctx, p)`. Reading in `Publish` rather than
`handlePublishMessage` covers the in-process callers too.

`msg.SenderID` is safe as a key: the executor supplies the agent's
framework-known registered id and never forwards an LLM-supplied value —
the RFC 0011 §"DM gate-bypass" invariant the publisher's own docstring
pins.

### Edge cases, each failing closed

- **Several people dispatching to one agent.** Two humans publish into
  one channel; both fan out to agent X, both write `(C, X)`.
  Last-write-wins would mis-attribute. Instead: a second dispatch under
  a *different* principal marks the entry **ambiguous**, and an
  ambiguous entry emits nothing. The reply collapses to `'local'` —
  today's behaviour, so ambiguity is a no-regression degradation rather
  than a wrong answer. A repeat dispatch under the *same* principal is
  not ambiguous and refreshes the entry.
- **A reply landing after someone else spoke.** Covered by the same
  rule: the intervening dispatch made the entry ambiguous, so the late
  reply degrades instead of inheriting the wrong person.
- **TTL.** Size it on the persona's worst realistic turn, the same
  budget `defaultSynthesisReplyTimeout` (120s) is sized against — the
  30s event timeout, up to two RFC 0051 reflexion rounds, dispatch and
  queue jitter. On expiry the entry is gone and the reply is `'local'`.
- **Deeper cascade.** X's re-stamped reply dispatches to Y *with* the
  principal on ctx, so `Dispatch` writes `(C, Y) → p` and attribution
  propagates along the causal chain, bounded by `cascade_depth`. This is
  intended; state it, because it means one authenticated publish can tag
  a whole discussion.
- **Autonomous / tick-origin publishes.** No entry was ever written, so
  nothing is inherited. Unchanged.
- **Convene and synthesis directives.** `handleConveneChannel`
  dispatches under the operator's principal, so the chair's synthesis
  reply inherits it — consistent with the other two dispatch origins in
  `dispatchOriginClassification`
  ([`internal/server/principal.go`](../../internal/server/principal.go)).
- **`auth.mode: disabled`.** No principal ever reaches a ctx, the table
  is never written, and the no-delta acceptance criterion holds at the
  byte level.
- **Bound and lifetime.** Size is `channels × members`, with lazy
  expiry on read plus a periodic sweep. Keep it **in memory only**: the
  session binding is persisted for continuity, but a *stale* attribution
  is a mis-attribution, so losing the table on restart (everything falls
  to `'local'`) is the safer failure. Single-orchestrator only — a
  multi-orchestrator deployment is out of scope at this scale and should
  be stated, not assumed.

### Residual risk, accepted

R-2 lets an agent's publish be stamped with the principal of the person
who caused it. A *compromised* persona could therefore write chosen
content into that person's tenant — a bounded **write** primitive, never
a read. That trust is already extended: the same agent already publishes
into the room and already writes the reply into memory. The read
boundary, which is the one strict equality defends, is untouched. A
route-table style test should pin that `Publish` is the only re-stamp
site, so a second one cannot be added by omission.

### Sequencing

Ships with [ISSUE-0123](ISSUE-0123-per-speaker-interaction-scope.md)
(R-1). R-2 alone still lets the close-time aggregate cross speakers;
R-1 alone creates a systematic `'local'` record holding every agent turn
in every room. Together the relayed turn carries the causal principal
*and* lands in that principal's own record.

## Notes

> 2026-08-06 — designed out of the ISSUE-0082 Part 2 review deferral.
> **v0.4.0 work: not implemented.** v0.3.14 is uncut and its own PR 2
> ([#820](https://github.com/mkhomutov/Persatrix/pull/820)) — which
> lands the `withRequestPrincipal` producer this design builds on — is
> still open.
>
> Live observation is
> [MT-MEMORY-GROUP-TENANT-001](../manual-tests/MT-MEMORY-GROUP-TENANT-001.md).
>
> 2026-08-07 — **CONFIRMED LIVE, with a magnitude.** Ran on Anthropic
> under `auth.mode: enabled` (`alice` → `alice-person`, operator) in
> `group:planning` with three personas. Read off the `channel.dispatch`
> spans — the `principal.id` attribute v0.3.14 PR 1 added for exactly
> this:
>
> | message | origin | dispatches | `principal.id` |
> |---|---|---|---|
> | `457a4612`, `03630550` | alice, authenticated | 6 | **`alice-person`** on all |
> | `a80b582b`, `acd79138`, `aa77ece4` | persona replies | 9 | **absent on all** |
>
> **9 of 15 dispatches in one interaction descending from an
> authenticated publish carried no tenant** — 60%. `a80b582b` is
> iron-fox's *"Nova Sparrow — you've just been looped in by Alice…"*,
> a persona relaying Alice's context to two other personas with the
> tenant stripped. That is the residual, exactly as described.
>
> **Correction to the note above: the residual is NOT readable off
> storage, and R-1 is why.** Every stored row in that run — 9 episodes
> and 28 facts across the three personas — read `principal_id =
> 'alice-person'`. **Zero `local` rows**, despite 9 tenant-less
> dispatches. Nothing is written per turn; the only write is at close,
> and the close-time aggregate takes one principal for the whole record
> ([ISSUE-0123](ISSUE-0123-per-speaker-interaction-scope.md)). So R-1
> *masks* R-2 in storage: the relayed turns are ingested with no tenant,
> then silently re-attributed to whoever the close bound. Any Leg that
> tries to detect R-2 by grouping `principal_id` will read clean and
> conclude wrongly. **The wire is the only instrument.** The MT's Leg 2
> is corrected accordingly.
>
> Two preconditions the run discovered, both now in the MT: agent
> replies must take the **concurrent re-fanout** path (`floor_control:
> false`) — under floor control `ChannelRouter.Publish`'s deferred-fanout
> skip suppresses a floor-turn reply's re-fanout, so agent publishes
> never reach `Dispatch` and the arc shows zero tenant-less hops; and the
> collector's tail sampling is **1% probabilistic**, so the dispatch
> spans are dropped unless it is raised for the measurement.

> 2026-08-25 — **PR 1 of the fix is open** (`feature/v0315-issue0124-attribution-store`,
> the [residuals PR plan](ISSUE-0082-residuals-pr-plan.md) PR 1): the
> attribution table itself —
> [`internal/channels/principal_attribution.go`](../../internal/channels/principal_attribution.go)
> — plus the write at the dispatch chokepoint and the wiring that hands the
> instance to the dispatcher. **Nothing reads it**; the re-stamp in
> `ChannelRouter.Publish` is PR 2, so behaviour is unchanged and the wire is
> byte-identical either way (pinned, not assumed). The design above is
> implemented as written, with **three refinements the implementation forced**,
> all narrowing rather than widening what gets attributed:
>
> - **The write is on the DELIVERED path, and only for a stimulus the
>   orchestrator asked a turn for.** The design says "record when
>   `PrincipalFromContext(ctx)` is non-empty"; the record is placed after the
>   receiver's ack instead, because a dispatch that was dropped (unregistered
>   recipient), refused (the servicer's queue-full / pre-ingest ack with
>   `success=false`) or never dialled leaves the agent holding nothing.
>   Delivery is not the whole test, though: that ack is **pre-ingest** — the
>   servicer returns as soon as the wake is accepted and the response gate runs
>   later, inside the event loop — and the fanout deliberately delivers to
>   members the gate will silence (an unmentioned `when_mentioned` member, a
>   directed-elsewhere `always` member) so they ingest the room rather than
>   going amnesiac. Those hold the stimulus and never answer it, so an entry
>   for one would stay live for a full turn budget and be inherited by whatever
>   that agent published next, e.g. an autonomous tick — a mis-attribution of
>   content nobody caused, the one failure mode this design is otherwise free
>   of. The write therefore follows `orderResponders`, the orchestrator's own
>   superset of the gate's respond-true set, carried to the chokepoint as
>   `DispatchEnvelope.ExpectsReply`. **Residual, stated**: that election is a
>   superset, so an elected member whose RFC 0030 salience bid lands below
>   threshold still leaves an entry — an LLM-side judgement the orchestrator
>   cannot predict, bounded only by the TTL.
> - **An entry holds the LIVE STIMULI and derives its answer**, rather than
>   storing a principal beside an `ambiguous` flag. A pair resolves only when
>   exactly one stimulus is outstanding and it has a principal. The design
>   fixes the two-principals case but not what a THIRD dispatch does, and the
>   first shape answered that by latching the flag and refreshing its stamp on
>   every later write — which pinned a room in continuous conversation to
>   `'local'` for as long as the conversation lasted, since a cascade keeps
>   itself busy by construction. Deriving it instead makes ambiguity expire
>   with the stimuli that caused it: one message from a second speaker stops
>   mattering one turn budget later, and a busy room recovers on its own.
> - **An unauthenticated dispatch POISONS a live entry.** The design's
>   empty-principal write gate skipped those dispatches entirely, which left a
>   live authenticated entry to answer for a turn nobody authenticated caused —
>   the fresh-context origins `principal_context.go` enumerates
>   (`handleConveneChannel`, the synthesis-close timeout), every agent-origin
>   and autonomous turn. Such a dispatch is a real competing stimulus that
>   simply cannot name anyone, so it is recorded under the anonymous key: it
>   can make a pair ambiguous, never resolve one. It does **not create** a row,
>   so `auth.mode: disabled` still holds an empty table for the life of the
>   process.
>
> The TTL is its own constant (`principalAttributionTTL`, 120s) rather than an
> alias of `defaultSynthesisReplyTimeout`: they answer to the same reasoning
> today, but re-tuning how long a chair may take to synthesize must not
> silently re-tune how long a person stays answerable for what an agent says.
> The periodic sweep is interval-gated on the write path rather than a
> goroutine — a background sweeper would need a lifetime, a stop signal and a
> place in the shutdown ordering to reclaim a map bounded by
> `channels × members`. Both single-orchestrator and in-memory-only remain as
> designed, and are stated in the file rather than assumed. Gates:
> `internal/channels/principal_attribution_test.go`,
> `internal/channels/grpc_dispatcher_attribution_test.go`,
> `internal/channels/dispatch_expects_reply_test.go`.

> 2026-08-21 — **re-slotted v0.4.0 → v0.3.15** by the
> sequencing Amendment 2026-08-19 ([v0.3.x-sequencing.md](../v0.3.x-sequencing.md), landing with [#839](https://github.com/mkhomutov/Persatrix/pull/839));
> branch prefixes move `v040-` → `v0315-`. R-2 still ships first in the
> workstream — it is self-contained Go with no schema, and it removes the
> `local` pollution that R-1's keying would otherwise systematize. Unchanged
> by the [ISSUE-0131](ISSUE-0131-derived-memory-has-no-speaker-attribution.md) fold-in: the speaker axis is a persona-memory record
> shape, while this issue is a wire-attribution fix, and the two meet only at
> the close.
