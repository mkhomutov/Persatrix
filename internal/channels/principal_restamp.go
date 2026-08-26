package channels

// principal_restamp.go — ISSUE-0124 (ISSUE-0082 residual R-2) PR 2: the read
// side of the causal-attribution table, and the one site that INFERS a
// principal no caller ever presented. (The package's other [WithPrincipal]
// call, in synthesis_close.go, re-applies one the arming request DID present
// onto a timer goroutine's background context — it can name nobody new. The
// two-site allowlist is pinned in principal_restamp_test.go.)
//
// WHAT CHANGES HERE. PR 1 landed [PrincipalAttributionTable] as a producer with
// no consumer: the dispatch chokepoint records `(channel, agent) → principal`
// for every delivered stimulus the router elected a reply from, and nothing
// read it. This is the reader. When a persona's reply re-enters the
// orchestrator through `HTTPChannelPublisher` — a fresh UNAUTHENTICATED REST
// publish, the one hop that loses the tenant — the publish path asks the table
// who caused it and, on a live unambiguous hit, puts that principal back on the
// context. Every dispatch descending from the reply then emits
// `persatrix-principal` again ([GRPCMessageDispatcher.Dispatch]), so the whole
// cascade below a person's turn stays in that person's tenant instead of
// collapsing into the shared `'local'` bucket every agent-origin and autonomous
// turn resolves. Measured live 2026-08-07: 9 of 15 dispatches in one
// interaction descending from a single authenticated publish carried no
// principal — that 60% is what this closes.
//
// THE TWO GATES, AND WHY THE SECOND ONE IS THE TABLE ITSELF.
//
//  1. The context must carry NO principal. An authenticated publish is never
//     overridden: the caller's own verified identity outranks anything the
//     orchestrator could infer, and a re-stamp that could beat it would be a
//     way to move a human's turn into another tenant. A deeper cascade hop
//     already arrives WITH a principal (the re-stamped reply's fanout put it
//     there), so this gate is also what stops the chain from re-consuming the
//     table at every level.
//
//  2. The sender must be a registered agent — and the table key IS that proof,
//     which is why no separate check appears below. An entry exists only where
//     [GRPCMessageDispatcher.Dispatch] resolved the recipient through the
//     registry, found it healthy, dialled it, and got a delivery ack; every
//     miss path returns before the write. So a hit on `(channel, sender)` says
//     the orchestrator itself dispatched to this id as a registered agent
//     within the turn budget. That is strictly stronger than the alternative
//     available here — the `participant_type` claim on `msg.Metadata`, which
//     the REST handler overrides from the registry only on a HIT
//     ([Server.resolveSenderParticipantType]); on a registry miss or read
//     failure the claim is the CALLER's, and gating on it would either trust a
//     wire value or silently disable the re-stamp for the duration of a
//     registry hiccup. A human's participant id can never be in the table (the
//     registry holds agents), so gating on the key alone is not a widening.
//
// WHY THE READ CONSUMES. [PrincipalAttributionTable.TakeAttribution], never
// [PrincipalAttributionTable.Lookup]: an agent that publishes has answered
// whatever it was holding, and this read is the only evidence the orchestrator
// ever gets that a stimulus is SPENT rather than merely young. It is also the
// table's ONLY retirement mechanism — expiry disqualifies a stimulus from
// resolving but deliberately never removes it (the crossover rule in
// principal_attribution.go) — so without it, in exactly the rooms R-2 hurts
// most — a busy autonomous cascade, where the RFC 0052 convener cadence, a
// synthesis-close timeout or a chair escalation keeps re-dispatching
// principal-less forced turns — anonymous stimuli accumulate, every
// authenticated stimulus lands ambiguous, and the pair never resolves again.
// Retiring on the reply is what lets the next authenticated stimulus stand
// alone.
//
// IT CONSUMES EVEN WHEN THE PUBLISH IS REJECTED, and that is the fail-closed
// direction. This runs at the head of [ChannelRouter.publishCommit], before
// membership, cascade clamping and the reply budget can turn the publish away.
// A rejected publish is still an agent that ANSWERED — leaving its stimuli live
// would let the agent's next, unrelated publish inherit them, which is a
// mis-attribution. Retiring costs a missed attribution instead, and a miss is
// today's behaviour.
//
// WHERE IT SITS, AND WHY THERE IS ONLY ONE SITE. Inside `publishCommit` rather
// than in [ChannelRouter.Publish] and [ChannelRouter.PublishAsync] separately:
// both entry points funnel through it (as does [ChannelRouter.PublishAndAwait],
// via Publish), so a third one cannot be added without inheriting the re-stamp,
// and the table cannot be consumed twice for one publish. Reading in the router
// rather than in the REST handler is the issue's own instruction — it covers
// in-process callers too. Pinned structurally by
// principal_restamp_test.go's single-call-site test.
//
// It runs BEFORE the store commit, not just before fanout, which is deliberate
// beyond tidiness: `messages` gains a server-stamped `principal_id` in
// ISSUE-0130 shape (b) (channel store v11 → v12), and stamping it from a
// context that already carries the causal principal is what lets a REPLAYED
// relayed turn be attributed at catch-up. That is B1's column to add; the
// ordering here is what makes it possible rather than something B1 must undo.
//
// ACCEPTED RISK, STATED. This grants an agent a bounded WRITE into the tenant
// of the person who causally provoked it — the persona's reply, and everything
// the cascade below it derives, lands in that person's partition. It is never a
// READ: strict-equality recall still keys on the principal the persona binds
// from the header, and nothing here lets an agent NAME a principal. The write
// trust is already extended — the same agent already publishes into the room
// and already writes its own reply into memory. The primitive a compromised
// persona gets is "put chosen content in the tenant of someone it is already
// talking to", not "read that tenant".
//
// KNOWN GAP: single-orchestrator. The table is in-memory, so a reply routed to
// a different orchestrator than its stimulus finds no entry and degrades to
// `'local'` — a miss, not a wrong answer. Stated on the table itself
// (principal_attribution.go) and repeated here because this is the surface
// where an operator would notice it.

import (
	"context"

	"go.uber.org/zap"
)

// SetPrincipalAttribution wires the ISSUE-0124 causal-attribution table's READ
// side into the router. Pass the same instance handed to the dispatcher via
// [WithPrincipalAttribution] — the dispatcher writes it, the router consumes
// it, and two instances would leave the re-stamp permanently empty.
//
// Optional: a router without a table never re-stamps, which is the
// pre-ISSUE-0124 behaviour byte for byte. That is the posture for every
// deployment that wires no dispatcher table (and for the router's own unit
// tests, which construct without one).
func (r *ChannelRouter) SetPrincipalAttribution(t *PrincipalAttributionTable) {
	r.attribution = t
}

// restampCausalPrincipal returns ctx carrying the principal that caused this
// publish, when the orchestrator can name one, and ctx untouched otherwise —
// the file header has the full contract. Called once per publish, from
// [ChannelRouter.publishCommit].
//
// The three degradations the table can return (no entry, ambiguous, expired)
// are indistinguishable here on purpose: each leaves the context exactly as it
// arrived, so the publish keeps resolving `'local'` — the behaviour before this
// PR. Only an unambiguous live hit changes anything.
func (r *ChannelRouter) restampCausalPrincipal(ctx context.Context, msg ChannelMessage) context.Context {
	// Gate 1: never override an authenticated caller, and never re-consume the
	// table for a cascade hop that already carries the principal.
	if PrincipalFromContext(ctx) != "" {
		return ctx
	}
	// Consuming read: the agent has spoken, so retire what it answered whether
	// or not the orchestrator can name who caused it. Nil-safe on the table
	// (an unwired deployment reads nothing and retires nothing).
	principal, ok := r.attribution.TakeAttribution(msg.ChannelID, msg.SenderID)
	if !ok {
		return ctx
	}
	// DEBUG, not INFO: this fires once per relayed turn in every multi-agent
	// room, so it is a trace-the-cascade line rather than an event. The
	// operator-facing signal for the same fact is the `principal.id` attribute
	// the dispatch span already carries (grpc_dispatcher.go) — which is what
	// the live MT reads to prove R-2 closed, and it is now populated on the
	// hops that used to be blank.
	r.logger.Debug("channels: re-stamped an agent publish with the principal that caused it (ISSUE-0124 R-2)",
		zap.String("channel_id", msg.ChannelID),
		zap.String("message_id", msg.ID),
		zap.String("sender_id", msg.SenderID),
		zap.String("principal_id", principal),
	)
	return WithPrincipal(ctx, principal)
}
