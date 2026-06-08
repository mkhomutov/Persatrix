package channels

import (
	"context"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

// governance.go is the single source of truth for the RFC 0030 deterministic
// governance-layer composition in the channel publish path (§B). The per-layer
// mechanics live in their own files (cascade_depth.go, reply_budget.go,
// end_vote.go); this file documents how they COMPOSE — the evaluation order, the
// short-circuit rule, and the shared telemetry vocabulary — so a reader does not
// have to reconstruct the contract from four scattered call sites.
//
// # Composition order (§B)
//
// RFC 0030 §B orders the layers cheap-and-unfailable → expensive-and-judgement:
// Layer 0 (depth) → Layer 1 (cost) → Layer 2 (reply budget) → Layer 2.5 (floor)
// → Layer 3 (respond policy) → Layer 4 (end-vote) → Layer 5/6 (deferred). The
// composition rule: a publish proceeds only if every ACTIVE layer admits it; a
// lower-layer drop short-circuits the higher layers and increments
// `governance_drop{layer}`; higher layers fail safely down to the lower ones
// (Layer 0 is always on).
//
// # How that maps onto [ChannelRouter.Publish]
//
// The channel publish path enforces the order in two phases, because the layers
// answer two different questions — "should this message EXIST in history?" vs.
// "should it CASCADE to other personas?":
//
//   - PRE-PERSISTENCE rejection (the message never enters channel history):
//     Layer 2 (reply budget) via [ChannelRouter.publishWithReplyBudget]. An
//     over-budget (K+1)th publish is rejected with [ErrParticipantBudgetExhausted]
//     (REST 429) before [ChannelStore.PublishMessage], so it never pollutes
//     future memory recall (§F). This runs FIRST, so a reply-budget drop
//     short-circuits every later layer and the fanout entirely.
//   - POST-PERSISTENCE fanout suppression (the publish is a valid 2xx message;
//     only the cascade is terminated): Layer 4 (end-vote close / duplicate-vote
//     suppression), then Layer 0 (cascade-depth cap), then Layer 2.5 (floor
//     control). Each returns early from Publish before [ChannelRouter.fanout],
//     so the first one to fire short-circuits the rest.
//
// Layer 0's cap deliberately persists the message and only suppresses fanout
// (the publish itself is a 2xx; the RFC 0011 amendment chose persist-then-cap
// over reject, see cascade_depth.go) — a documented divergence from the §B
// diagram's "drop", preserved here unchanged.
//
// Layer 1 (cost ceiling) is NOT a channel-publish-path check. It is enforced
// UPSTREAM in the wallet ([WalletService.AcquireLease], internal/wallet): a lease
// denied with `INTERACTION_BUDGET_EXHAUSTED` means the persona's LLM call never
// happens, so no reply is generated and nothing reaches this publish path to be
// dropped. That is the cross-process form of the same short-circuit — the cost
// ceiling fails closed before the reply exists (GL5). Its `governance_drop{layer=cost}`
// counter and the `cost_tokens_per_interaction` histogram belong to the wallet
// metrics surface and land with the orchestrator→agent budget-stamping wiring
// (deferred from this PR — the wallet holds no metrics handle today and the layer
// is inert until the `interaction_id` producer lands; see the governance-layers
// PR plan).
//
// # Inert in production (today)
//
// Every layer keyed on `interaction_id` (cost, reply budget, end-vote) is inert
// on real traffic because no orchestrator- or agent-side producer writes
// `interaction_id` onto publish metadata yet (readInteractionID returns "" — see
// interaction_id.go). The layers are wired, composed, and tested ahead of that
// producer, not yet load-bearing. With every layer at its default
// (uncapped/untracked/no votes) the publish path is behaviourally identical to
// v0.3.7 — the back-compat invariant the composition tests pin (GL1).

// Governance-layer label values for the `channel.conversation.governance_drop`
// counter's `layer` attribute and the matching structured-log / span field. One
// vocabulary, shared by every drop site, so the dashboard query
// (`governance_drop{layer="reply_budget"}`) and the trace query
// (`conversation.governance.layer="reply_budget"`) use identical strings.
//
// `governanceLayerCost` has no emission site in the channels package (Layer 1 is
// wallet-side, see the file doc); it is named here so the vocabulary is complete
// and the future wallet/composition wiring uses the same string.
const (
	governanceLayerDepth       = "depth"
	governanceLayerCost        = "cost"
	governanceLayerReplyBudget = "reply_budget"
	governanceLayerEndVote     = "end_vote"
)

// governanceSpanLayerAttr is the trace-correlation span attribute key emitted on
// every governance drop (§L): `conversation.governance.layer=<layer>` lets an
// operator find "all publishes dropped by Layer 2 in #planning today" with one
// Jaeger/Tempo query instead of a log grep.
const governanceSpanLayerAttr = "conversation.governance.layer"

// annotateGovernanceDropSpan stamps the `conversation.governance.layer` attribute
// on the span currently in ctx (the inbound REST/gRPC publish span, when one is
// recording) so a governance drop is correlatable in a trace, not only in logs
// (§L). Cheap and safe on the hot path: [trace.SpanFromContext] returns a no-op
// span when ctx carries none, and SetAttributes on a non-recording span is a
// no-op — so an unsampled or span-less publish pays nothing.
func annotateGovernanceDropSpan(ctx context.Context, layer string) {
	span := trace.SpanFromContext(ctx)
	if !span.IsRecording() {
		return
	}
	span.SetAttributes(attribute.String(governanceSpanLayerAttr, layer))
}
