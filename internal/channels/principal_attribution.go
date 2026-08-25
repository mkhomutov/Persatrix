package channels

// principal_attribution.go — ISSUE-0124 (ISSUE-0082 residual R-2) PR 1: the
// server-held record of which person caused a given agent to be spoken to.
//
// THE PROBLEM. The tenant axis survives every in-process hop and dies on the
// one hop that leaves the process and comes back. When an authenticated
// person publishes, [WithPrincipal] puts their verified principal on the
// request context and every dispatch descending from it emits
// `persatrix-principal`. But a persona's REPLY re-enters the orchestrator
// through `HTTPChannelPublisher` as a fresh UNAUTHENTICATED REST publish, so
// the whole fanout below it carries no tenant — measured live on 2026-08-07 at
// 9 of 15 dispatches in a single interaction descending from one authenticated
// publish. Agent B's restatement of A's disclosure is then written to the
// shared `'local'` tenant, which every agent-origin and autonomous turn
// resolves, and RFC 0049 Phase 1 facts are cross-room by default.
//
// WHY THE OBVIOUS FIX IS UNAVAILABLE. Having the persona echo the principal
// back on its publish would make the orchestrator trust an agent-supplied
// identity claim, and the persona binds `principal_scope` from that value with
// STRICT-EQUALITY recall — so an unauthenticated caller could name any tenant
// and read it. That trades a write leak for a cross-tenant READ primitive,
// which is strictly worse. The same objection kills the tempting refinement of
// having the agent echo the stimulus MESSAGE ID: an agent sees other members'
// message ids in channel history, so echoing a CHOSEN id resolves to a CHOSEN
// principal — the read primitive again, one indirection along. Any correlation
// key the agent supplies is disqualified.
//
// THE SHAPE THAT IS LEFT. State the orchestrator already knows, held
// server-side, never accepted from the wire. [GRPCMessageDispatcher.Dispatch]
// is the single chokepoint that knows both halves of one true statement — the
// orchestrator handed THIS agent THIS stimulus under THIS principal — so it
// records `(channel, agent) → principal` here, and PR 2 reads it in
// [ChannelRouter.Publish] to re-stamp an agent's reply with the principal that
// caused it.
//
// DORMANT IN THIS PR. Nothing reads the table yet, so behaviour is unchanged
// everywhere; the wire-level no-delta is pinned rather than asserted (see
// grpc_dispatcher_attribution_test.go). This mirrors the dormant-rail split
// v0.3.14 PR 1 / PR 2 used for the principal carrier itself.
//
// EVERY DEGRADATION FAILS CLOSED. Ambiguity, expiry and a missing entry all
// resolve nothing, and PR 2's caller will leave the publish at `'local'` — the
// behaviour today. So a wrong answer is never the failure mode; a *missed*
// attribution is, and that is a no-regression.
//
// IN MEMORY ONLY, AND SINGLE-ORCHESTRATOR. The session binding is persisted
// for continuity, but a STALE attribution is a MIS-attribution, so losing the
// table on restart (everything falls back to `'local'`) is the safer failure
// and persistence would be the wrong trade. For the same reason a
// multi-orchestrator deployment is out of scope: a reply routed to a different
// orchestrator than the stimulus finds no entry and degrades. Stated rather
// than assumed — it is a real limit of the design, not an oversight.

import (
	"sync"
	"time"
)

// principalAttributionTTL bounds how long a dispatch can still explain a
// reply. Sized on the persona's worst realistic turn — the same budget
// [defaultSynthesisReplyTimeout] is sized against: the 30s persona event
// timeout, up to two RFC 0051 reflexion rounds, plus dispatch and queue
// jitter. Deliberately its own constant rather than an alias of that one:
// they answer to the same reasoning today, but re-tuning how long a chair may
// take to synthesize must not silently re-tune how long a person stays
// answerable for what an agent says.
//
// The cost of too SHORT is a missed attribution (the reply degrades to
// `'local'`, today's behaviour); the cost of too LONG is that a genuinely
// unrelated later publish by the same agent inherits a stale principal. The
// second is the one worth avoiding, which is why this is a turn budget and not
// a session lifetime.
const principalAttributionTTL = 120 * time.Second

// principalAttributionKey identifies one dispatch relationship: the room the
// stimulus was published into, and the agent it was handed to. A struct key
// rather than a joined string so no separator can ever be forged into an id
// (participant ids and channel ids have different grammars, and concatenation
// is how that kind of bug gets in).
type principalAttributionKey struct {
	channelID string
	agentID   string
}

// principalAttributionEntry is what the orchestrator knows about one such
// relationship. `ambiguous` records that two DIFFERENT principals had live
// stimuli out to this agent at once, which makes the agent's next reply
// unattributable rather than attributable to the more recent of them.
type principalAttributionEntry struct {
	principal    string
	dispatchedAt time.Time
	ambiguous    bool
}

// PrincipalAttributionTable holds the per-`(channel, agent)` causal
// attribution described in the file header. Construct one with
// [NewPrincipalAttributionTable] and wire it into the dispatcher with
// [WithPrincipalAttribution]; PR 2 wires the same instance into the router as
// the read side.
//
// All methods are safe on a nil receiver, so a deployment that never wires one
// (channels disabled, or a test that does not care) needs no guards at the
// call sites.
type PrincipalAttributionTable struct {
	mu      sync.Mutex
	entries map[principalAttributionKey]principalAttributionEntry

	// ttl is the entry lifetime, [principalAttributionTTL] in production.
	ttl time.Duration
	// now is the clock, overridable in tests (the activityNow pattern used
	// across this package) so expiry is asserted deterministically rather
	// than by sleeping through a two-minute TTL.
	now func() time.Time
	// lastSweep stamps the most recent eager sweep. Zero means "never swept",
	// which arms the first interval on the first write rather than at
	// construction — otherwise a test that replaces `now` after construction
	// would compare a fake clock against a real-time stamp.
	lastSweep time.Time
}

// NewPrincipalAttributionTable returns an empty table on the production clock
// and TTL.
func NewPrincipalAttributionTable() *PrincipalAttributionTable {
	return &PrincipalAttributionTable{
		entries: make(map[principalAttributionKey]principalAttributionEntry),
		ttl:     principalAttributionTTL,
		now:     time.Now,
	}
}

// Record notes that the orchestrator handed `agentID` a stimulus in
// `channelID` under `principal`. Called from the dispatch chokepoint on
// DELIVERED dispatches only — a stimulus the agent never ingested cannot cause
// a reply, and an entry left behind by an undelivered one would be inherited
// by that agent's next unrelated publish.
//
// An empty component writes nothing. The empty PRINCIPAL is the load-bearing
// case: it is what `auth.mode: disabled`, every unauthenticated caller and
// every agent/autonomous-origin turn resolve, so recording it would key the
// table on a value that means "no tenant at all" and hand the read side a hit
// that says nothing.
//
// The ambiguity rule, and its one deliberate imprecision: a repeat dispatch
// under the SAME principal refreshes the entry (it restates the same true
// fact), while one under a DIFFERENT principal marks it ambiguous — two people
// spoke to this agent inside one turn budget, so its next reply may be
// answering either. Ambiguity is then STICKY for the life of the entry: a
// third dispatch, even from the first principal, cannot establish that the
// pending reply is answering that principal. Because every write refreshes the
// stamp, a room in continuous two-person conversation stays ambiguous while it
// lasts — measurably conservative, and in the safe direction. Ambiguity dies
// with the entry, so once both stimuli age out the next dispatch starts a
// fresh unambiguous fact and a busy room never latches itself into permanent
// `'local'`.
func (t *PrincipalAttributionTable) Record(channelID, agentID, principal string) {
	if t == nil || channelID == "" || agentID == "" || principal == "" {
		return
	}
	now := t.now()

	t.mu.Lock()
	defer t.mu.Unlock()
	t.sweepLocked(now)

	key := principalAttributionKey{channelID: channelID, agentID: agentID}
	next := principalAttributionEntry{principal: principal, dispatchedAt: now}
	if prev, ok := t.entries[key]; ok && !t.expiredLocked(prev, now) {
		if prev.ambiguous || prev.principal != principal {
			next = principalAttributionEntry{dispatchedAt: now, ambiguous: true}
		}
	}
	t.entries[key] = next
}

// Lookup returns the principal a live, unambiguous dispatch to `agentID` in
// `channelID` was made under. The second return is false when there is no
// entry, when the entry has aged past the TTL, or when it is ambiguous — the
// three ways this fails closed, all of which leave the caller at `'local'`.
//
// Expiry is enforced here rather than only by the sweep, so a read can never
// resolve a stale attribution however long the sweep interval is; the expired
// row is dropped on the way out.
//
// No production caller in this PR — PR 2 adds the one at
// [ChannelRouter.Publish], which is pinned as the only re-stamp site.
func (t *PrincipalAttributionTable) Lookup(channelID, agentID string) (string, bool) {
	if t == nil {
		return "", false
	}
	now := t.now()

	t.mu.Lock()
	defer t.mu.Unlock()

	key := principalAttributionKey{channelID: channelID, agentID: agentID}
	entry, ok := t.entries[key]
	if !ok {
		return "", false
	}
	if t.expiredLocked(entry, now) {
		delete(t.entries, key)
		return "", false
	}
	if entry.ambiguous {
		return "", false
	}
	return entry.principal, true
}

// expiredLocked reports whether an entry has reached its TTL. At exactly the
// TTL it is already gone: the budget is how long a stimulus can still explain
// a reply, so the boundary belongs to the safe side. Caller holds mu.
func (t *PrincipalAttributionTable) expiredLocked(e principalAttributionEntry, now time.Time) bool {
	return now.Sub(e.dispatchedAt) >= t.ttl
}

// sweepLocked drops every expired entry, at most once per TTL. Caller holds mu.
//
// Lazy expiry on read is what keeps the table CORRECT; this is what keeps it
// SMALL, and it is not redundant: the read side only ever looks up agents that
// publish, so an agent dispatched to in a room it never speaks in would hold
// its row for the life of the process. Piggybacked on the write path rather
// than run from a goroutine — a background sweeper would need a lifetime, a
// stop signal and a place in the shutdown ordering, all to reclaim a map whose
// bound is `channels × members`. Interval-gated, so the amortized cost on the
// dispatch path is a clock comparison.
func (t *PrincipalAttributionTable) sweepLocked(now time.Time) {
	if !t.lastSweep.IsZero() && now.Sub(t.lastSweep) < t.ttl {
		return
	}
	t.lastSweep = now
	for key, entry := range t.entries {
		if t.expiredLocked(entry, now) {
			delete(t.entries, key)
		}
	}
}

// len reports the number of rows held, expired ones included. Test-only: it is
// how the dormancy and sweep pins observe that nothing was written, which an
// absence-only assertion on [Lookup] cannot distinguish from a row that was
// written and then correctly refused.
func (t *PrincipalAttributionTable) len() int {
	if t == nil {
		return 0
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	return len(t.entries)
}
