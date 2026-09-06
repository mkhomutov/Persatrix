# ISSUE-0124 — the R-2 design record (record)

**Companion to**: [ISSUE-0124](ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md)
**Covers**: why the fix has the shape it has — the two unavailable options and the one that is left
**Release**: v0.3.15 *Who said what* — shipped and verified live

Split out of [ISSUE-0124](ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md)
on 2026-09-03, when the issue stood at **2 999/3 000 words** and its closure
note would not fit. Splitting rather than trimming follows the precedent this
issue's own parent set ([ISSUE-0082 Part 2](ISSUE-0082-part2-v0314-build-log.md)):
the reasoning below is *why the fix has the shape it has*, and a resolved issue
that has lost that is a worse record than a longer one.

The fix shipped in v0.3.15 and was verified live — see the issue's closure note.

---

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
- **`auth.mode: disabled`.** No principal ever reaches a ctx, so every
  dispatch records the anonymous stimulus and **nothing the table holds
  can ever resolve**. The no-delta acceptance criterion holds at the byte
  level; the residual cost is one anonymous-only row per dispatched-to
  pair, inside the bound below and reclaimed by the sweep once traffic
  stops. A deployment that wants no table at all declines to wire one.
- **Expiry is not the only retirement.** Ageing out recovers a pair only
  when the competing stimulus stops being restated. That holds for a
  second human speaker, but not for the orchestrator's own principal-less
  forced turns — the RFC 0052 convener cadence, a synthesis-close
  timeout, a chair escalation descending from either — which recur faster
  than the turn budget, keep an anonymous stimulus permanently live, and
  would leave such a room forever ambiguous with a row the sweep can
  never reclaim. So the re-stamp read (`TakeAttribution`) also **retires
  the stimuli the reply answered**: an agent that publishes has answered
  what it was holding, the only evidence the orchestrator gets that a
  stimulus is *spent* rather than merely young. Irreducible remainder,
  stated: an authenticated stimulus racing an **unanswered** anonymous
  one is genuinely ambiguous and must stay so — ranking them needs a
  correlation key from the agent, which is disqualified above.
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
